"""
向量存储与检索模块

存储方案：SQLite BLOB 列存 float32 序列化向量
  选它而非 FAISS / Chroma 的理由：零新增依赖、与 document_info 同库便于 JOIN 取文件名。
  本模块对外只暴露少数几个函数，未来语料上到十万级时可整体替换为 sqlite-vec / FAISS，
  上层 rag_pipeline 无感知。

性能关键：进程内向量矩阵缓存
  若每次检索都从 SQLite 全量读取 embedding 再反序列化，
  1 万个分块 × 512 维 × 4 字节 = 20MB，**每个请求都要重新读一遍**，是性能灾难。
  因此把整个矩阵缓存在进程内存中，仅在索引变更时失效重建。
"""
import logging
import threading
from typing import List, Dict, Optional, Tuple

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DocumentChunk, DocumentInfo, DocIndexState

logger = logging.getLogger(__name__)


# ==================== 进程内向量矩阵缓存 ====================

class VectorCache:
    """
    向量矩阵缓存。

    _matrix: shape=(N, dim) float32，所有分块的归一化向量
    _meta:   长度 N 的列表，与 _matrix 行一一对应，含分块元信息
    """

    def __init__(self):
        self._matrix: Optional[np.ndarray] = None
        self._meta: Optional[List[Dict]] = None
        self._dirty: bool = True
        self._model_tag: Optional[str] = None

        # 保护 _matrix / _meta / _dirty 这一组状态。
        # 为什么必须加：路由改为同步 def 后请求跑在线程池里，可能出现
        #   ① 多个线程同时发现缓存脏了，各自全量读一遍 embedding（20MB 级）
        #   ② 一个线程正在重建时另一个线程 invalidate()，失效被覆盖丢失
        #   ③ 最隐蔽的：A 线程读到新 _matrix、B 线程随即重建，
        #      A 再读 _meta 就拿到**新元信息配旧矩阵**，行号错位 → 检索结果张冠李戴
        # 所以 get() 必须在**同一把锁内**把两者一起取出来返回。
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """标记缓存失效（索引写入/删除后调用）"""
        with self._lock:
            self._dirty = True

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return not self._dirty and self._matrix is not None

    def get(self, db: Session) -> Tuple[Optional[np.ndarray], List[Dict]]:
        """获取向量矩阵与元信息，必要时重建

        持锁返回 —— 保证矩阵与元信息来自同一次加载，行号一一对应。
        重建期间其他线程会等待，这是**期望行为**：
        总比 5 个线程各读一遍 20MB 要好，也比拿到错配数据要好。
        """
        with self._lock:
            if self._dirty or self._matrix is None:
                self._reload(db)
            return self._matrix, self._meta or []

    def _reload(self, db: Session) -> None:
        """从数据库全量加载向量与元信息"""
        rows = (
            db.query(DocumentChunk, DocumentInfo.file_name)
            .join(DocumentInfo, DocumentChunk.doc_id == DocumentInfo.id)
            .order_by(DocumentChunk.doc_id, DocumentChunk.chunk_index)
            .all()
        )

        matrix_rows: List[np.ndarray] = []
        meta: List[Dict] = []
        dim = None

        for chunk, file_name in rows:
            if not chunk.embedding:
                continue
            vec = np.frombuffer(chunk.embedding, dtype=np.float32)
            if dim is None:
                dim = len(vec)
            if len(vec) != dim:
                logger.warning(f"分块 {chunk.id} 向量维度 {len(vec)} 与预期 {dim} 不一致，已跳过")
                continue
            matrix_rows.append(vec)
            meta.append({
                "chunk_id": chunk.id,
                "doc_id": chunk.doc_id,
                "chunk_index": chunk.chunk_index,
                "chunk_text": chunk.chunk_text,
                "chunk_text_raw": chunk.chunk_text_raw,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "page_no": chunk.page_no,
                "file_name": file_name,
                # 方案 B：版本与来源 —— 检索输出前据此核对是否仍是当前有效版本
                "revision": chunk.revision,
                "source_kind": chunk.source_kind or "text",
            })

        if matrix_rows:
            self._matrix = np.vstack(matrix_rows).astype(np.float32)
        else:
            self._matrix = np.zeros((0, dim or 0), dtype=np.float32)

        self._meta = meta
        self._dirty = False
        self._model_tag = rows[0][0].embed_model if rows else None

        logger.info(f"向量缓存已加载：{len(meta)} 个分块，维度 {dim}，"
                    f"内存占用约 {self._matrix.nbytes / 1024 / 1024:.2f} MB")

    @property
    def model_tag(self) -> Optional[str]:
        return self._model_tag


_cache = VectorCache()


def invalidate_cache() -> None:
    """使向量缓存失效（供索引写入后调用）"""
    _cache.invalidate()


def get_cache() -> VectorCache:
    return _cache


# ==================== 写入 ====================

def delete_doc_chunks(db: Session, doc_id: int, commit: bool = True) -> int:
    """
    删除某文档的全部分块。

    ⚠️ plan B 约束：索引替换路径**禁止调用本函数**（它会提前 commit，
    破坏"删除+写入+状态在同一个事务"的原子性）。请改用 replace_chunks_atomic()。
    本函数仅保留给「文档永久删除」等确实需要单独清除分块的场景。
    """
    n = db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc_id).delete()
    if commit:
        db.commit()
    if n:
        logger.info(f"已清除文档 {doc_id} 的 {n} 个旧分块")
    return n


def upsert_chunks(db: Session, doc_id: int, chunks: List[Dict],
                  embeddings: Optional[np.ndarray], model_tag: str) -> int:
    """
    旧接口：先删后建（不校验版本）。保留给历史脚本调用。

    注意：它与 replace_chunks_atomic 的唯一区别是不做版本/删除状态校验，
    因此**不能**用于 OCR/编辑后的索引重建 —— 那会把过期内容重新发布。
    """
    db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc_id).delete()
    for i, ch in enumerate(chunks):
        _add_chunk(db, doc_id, ch, embeddings, i, model_tag)
    db.commit()
    _cache.invalidate()
    logger.info(f"文档 {doc_id} 写入 {len(chunks)} 个分块（未校验版本，仅供旧脚本）")
    return len(chunks)


def _add_chunk(db: Session, doc_id: int, ch: Dict,
               embeddings: Optional[np.ndarray], i: int,
               model_tag: str, revision: Optional[int] = None) -> None:
    """构造并挂载一条分块记录（不提交，由调用方统一提交）"""
    emb_blob = None
    emb_dim = None
    if embeddings is not None and i < len(embeddings):
        vec = np.asarray(embeddings[i], dtype=np.float32)
        emb_blob = vec.tobytes()
        emb_dim = len(vec)

    db.add(DocumentChunk(
        doc_id=doc_id,
        chunk_index=ch["chunk_index"],
        chunk_text=ch["chunk_text"],
        chunk_text_raw=ch.get("chunk_text_raw"),
        char_start=ch.get("char_start"),
        char_end=ch.get("char_end"),
        page_no=ch.get("page_no"),
        embedding=emb_blob,
        embed_dim=emb_dim,
        embed_model=model_tag if emb_blob else None,
        revision=revision,
        source_kind=ch.get("source_kind") or "text",
    ))


def replace_chunks_atomic(
    db: Session,
    doc_id: int,
    chunks: List[Dict],
    embeddings: Optional[np.ndarray],
    model_tag: str,
    revision: Optional[int] = None,
) -> Dict:
    """
    **索引的唯一替换入口**：在单个事务内完成「旧分块删除 → 新分块写入 → 索引状态更新」。

    为什么必须是单个事务：
      旧实现先 delete+commit 再 insert+commit，两次提交之间存在一个窗口，
      此时文档处于"旧分块已删、新分块未建"的状态 —— 检索会漏召回，
      进程崩溃则留下永久空状态。这里改为全程一个事务，只 commit 一次。

    事务内二次校验（任务书 3.2）：
      索引在后台线程生成新分块期间，用户可能已经删除文档或发布了新版本。
      在写入前再校验一次 `current_revision == revision` 且未删除，
      否则整事务回滚并把原因返回给调用方，由上层决定是否重新触发。

    参数: chunks     — 完整分块列表（正文 text 与字段 fields 混合，每条带 source_kind）
          embeddings — 与 chunks **等长对齐**的向量矩阵（None 表示仅存文本）
          revision   — 预期的当前有效版本号；索引成功时写入 indexed_revision
    返回: {"ok":bool, "chunk_count":int, "reason":str|None}
          reason: version_changed / deleted / document_missing
    """
    # Acquire SQLite's write transaction before re-reading the version; an ORM
    # identity-map read alone can return an old object after lengthy inference.
    db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).update(
        {DocumentInfo.id: DocumentInfo.id}, synchronize_session=False)
    doc = db.query(DocumentInfo).populate_existing().filter(DocumentInfo.id == doc_id).first()
    if doc is None:
        db.rollback()
        return {"ok": False, "chunk_count": 0, "reason": "document_missing"}

    reason = None
    if doc.deleted_at is not None:
        reason = "deleted"
    elif revision is not None and doc.current_revision != revision:
        reason = "version_changed"

    if reason:
        db.rollback()
        logger.warning(
            f"[索引] 文档 {doc_id} 替换中止（{reason}）："
            f"预期 r{revision}，当前 r{doc.current_revision}"
        )
        return {"ok": False, "chunk_count": 0, "reason": reason}

    try:
        db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc_id).delete()
        for i, ch in enumerate(chunks):
            _add_chunk(db, doc_id, ch, embeddings, i, model_tag, revision=revision)

        state = db.query(DocIndexState).filter(DocIndexState.doc_id == doc_id).first()
        if state is None:
            state = DocIndexState(doc_id=doc_id)
            db.add(state)
        state.status = 1
        state.chunk_count = len(chunks)
        state.embed_model = model_tag if embeddings is not None else None
        state.error_msg = None
        state.indexed_revision = revision

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[索引] 文档 {doc_id} 原子替换失败已回滚: {e}", exc_info=True)
        return {"ok": False, "chunk_count": 0, "reason": "exception"}

    # 提交成功后才失效缓存 —— 事务未提交前失效，会把回滚掉的脏数据读进来
    _cache.invalidate()
    return {"ok": True, "chunk_count": len(chunks), "reason": None}


def set_index_state(db: Session, doc_id: int, status: int, chunk_count: Optional[int] = None,
                    embed_model: Optional[str] = None, error_msg: Optional[str] = None,
                    indexed_revision: Optional[int] = None) -> None:
    """更新文档索引状态（0待索引 / 1成功 / 2失败）"""
    state = db.query(DocIndexState).filter(DocIndexState.doc_id == doc_id).first()
    if state is None:
        state = DocIndexState(doc_id=doc_id)
        db.add(state)
    state.status = status
    if chunk_count is not None:
        state.chunk_count = chunk_count
    if embed_model is not None:
        state.embed_model = embed_model
    if indexed_revision is not None:
        state.indexed_revision = indexed_revision
    state.error_msg = error_msg
    db.commit()


# ==================== 可用范围与版本校验（任务书 3.2） ====================

def get_eligible_doc_ids(db: Session, doc_ids: Optional[List[int]] = None) -> List[int]:
    """
    当前**可安全召回**的文档范围。

    条件同时满足才算可用：
      ① 未被软删除           deleted_at IS NULL
      ② 有当前有效结果        status=1 且 current_revision 非空
      ③ 索引成功且指向当前版本  doc_index_state.status=1 且 indexed_revision=current_revision

    ③ 是关键：更新中/失败的当前版本**不用旧索引兜底**（否则会拿旧版本内容
    冒充当前版本回答），这类文档根本不进候选集，界面另行提示"部分文档索引未就绪"。

    参数: doc_ids — None 表示不限（返回全部可用文档）；
                    [] 表示明确指定空集合 → 返回 []，**绝不扩大为全部**
    返回: 可用文档 ID 列表
    """
    if doc_ids is not None and len(doc_ids) == 0:
        return []

    q = (
        db.query(DocumentInfo.id)
        .join(DocIndexState, DocIndexState.doc_id == DocumentInfo.id)
        .filter(
            DocumentInfo.deleted_at.is_(None),
            DocumentInfo.status == 1,
            DocumentInfo.current_revision.isnot(None),
            DocIndexState.status == 1,
            DocIndexState.indexed_revision == DocumentInfo.current_revision,
        )
    )
    if doc_ids is not None:
        q = q.filter(DocumentInfo.id.in_(doc_ids))
    return [row[0] for row in q.all()]


def filter_stale_hits(db: Session, hits: List[Dict]) -> List[Dict]:
    """
    输出前版本校验：剔除已过期（版本漂移）或已删除文档的分块。

    为什么检索已经过滤过还要再查一次库：
      内存缓存的 meta 是某一时刻的快照，缓存重建期间读到的数据可能是旧版本；
      命中到输出之间还有 LLM 生成的时间窗，期间文档可能已被删除或发布了新版本。
      因此终态必须以数据库为准，而不是信任缓存里的版本印记。
    """
    if not hits:
        return []

    doc_ids = sorted({h.get("doc_id") for h in hits if h.get("doc_id") is not None})
    if not doc_ids:
        return []

    rows = (
        db.query(DocumentInfo.id, DocumentInfo.current_revision, DocumentInfo.deleted_at)
        .filter(DocumentInfo.id.in_(doc_ids))
        .all()
    )
    current = {r[0]: r[1] for r in rows}
    deleted = {r[0] for r in rows if r[2] is not None}

    kept: List[Dict] = []
    dropped_deleted = 0
    dropped_stale = 0
    for h in hits:
        did = h.get("doc_id")
        if did in deleted:
            dropped_deleted += 1
            continue
        revision = h.get("revision")
        cur = current.get(did)
        # revision 为空是旧数据（迁移前建的分块），无法判定 → 保守剔除，
        # 复用"没有版本印记的旧索引冒充当前版本"正是要防的问题
        if revision is None or cur is None or revision != cur:
            dropped_stale += 1
            continue
        kept.append(h)

    if dropped_deleted or dropped_stale:
        logger.warning(
            f"输出前校验剔除：已删除 {dropped_deleted} 条、过期版本 {dropped_stale} 条"
        )
    return kept


# ==================== 检索 ====================

def search_vector(db: Session, query_vec: np.ndarray, top_n: int = 20,
                  doc_ids: Optional[List[int]] = None) -> List[Dict]:
    """
    向量检索：全量矩阵点积。

    查询向量与库内向量均已 L2 归一化，因此点积即余弦相似度，无需再算模长。

    参数: doc_ids — 可选，限定检索的文档范围
    返回: [{"chunk_id":.., "doc_id":.., "score":余弦相似度, "vec_rank":.., ...}, ...]
    """
    matrix, meta = _cache.get(db)
    if matrix is None or matrix.shape[0] == 0 or query_vec is None:
        return []

    sims = matrix @ np.asarray(query_vec, dtype=np.float32)

    # 按文档范围过滤（若指定）
    # ⚠️ 必须用 `is not None` 判断：空列表代表"明确不要任何文档"，
    #    若用真值判断就会被当成"没传过滤条件"而扩大成全库，这与任务书 3.2 相反。
    if doc_ids is not None:
        allowed = set(doc_ids)
        if not allowed:
            return []
        mask = np.array([m["doc_id"] in allowed for m in meta])
        if not mask.any():
            return []
        sims = np.where(mask, sims, -np.inf)

    # 取 Top-N：用 argpartition 做部分排序，避免全量排序开销
    n = sims.shape[0]
    k = min(top_n, n)
    if k < n:
        idx_part = np.argpartition(-sims, k - 1)[:k]
        order = idx_part[np.argsort(-sims[idx_part])]
    else:
        order = np.argsort(-sims)

    results: List[Dict] = []
    for rank, idx in enumerate(order, start=1):
        score = float(sims[idx])
        if not np.isfinite(score):
            continue
        item = dict(meta[int(idx)])
        item["score"] = round(score, 6)
        item["vec_rank"] = rank
        results.append(item)
    return results


def load_chunks_for_bm25(db: Session) -> List[Dict]:
    """
    加载全部分块文本，用于构建 BM25 索引。

    返回: [{"chunk_id":.., "doc_id":.., "chunk_text":.., "page_no":.., "file_name":..}, ...]
    """
    rows = (
        db.query(DocumentChunk, DocumentInfo.file_name)
        .join(DocumentInfo, DocumentChunk.doc_id == DocumentInfo.id)
        .order_by(DocumentChunk.doc_id, DocumentChunk.chunk_index)
        .all()
    )
    return [
        {
            "chunk_id": c.id,
            "doc_id": c.doc_id,
            "chunk_index": c.chunk_index,
            "chunk_text": c.chunk_text,
            "chunk_text_raw": c.chunk_text_raw,
            "char_start": c.char_start,
            "char_end": c.char_end,
            "page_no": c.page_no,
            "file_name": fn,
            "revision": c.revision,
            "source_kind": c.source_kind or "text",
        }
        for c, fn in rows
    ]


def get_index_status(db: Session) -> Dict:
    """
    索引状态统计，供 /api/rag/index/status 使用。

    能回答"哪些文档还没索引 / 索引失败"—— 这是 v1 方案缺失的可观测性。
    """
    # 回收站内的文档不参与任何统计与索引口径（任务书 3.2/3.4）
    alive = db.query(DocumentInfo).filter(
        DocumentInfo.deleted_at.is_(None), DocumentInfo.status == 1
    ).all()
    alive_ids = {d.id for d in alive}
    valid_ids = {d.id for d in alive if d.current_revision is not None}

    total_docs = len(alive)
    total_chunks = db.query(func.count(DocumentChunk.id)).scalar() or 0
    embedded_chunks = db.query(func.count(DocumentChunk.id)).filter(
        DocumentChunk.embedding.isnot(None)).scalar() or 0

    state_rows = db.query(DocIndexState).filter(
        DocIndexState.doc_id.in_(alive_ids) if alive_ids else DocIndexState.doc_id.is_(None)
    ).all()
    by_status = {0: 0, 1: 0, 2: 0}
    failed: List[Dict] = []
    indexed_ids = set()
    stale_ids: List[int] = []
    pending_ids: List[int] = []
    rev_of = {d.id: d.current_revision for d in alive}
    for s in state_rows:
        by_status[s.status] = by_status.get(s.status, 0) + 1
        if s.status == 1:
            # 索引成功但版本已漂移 → 该文档当前版本处于"索引未就绪"
            if s.indexed_revision == rev_of.get(s.doc_id):
                indexed_ids.add(s.doc_id)
            else:
                stale_ids.append(s.doc_id)
        elif s.status == 2:
            failed.append({"doc_id": s.doc_id, "error": s.error_msg})
        elif s.status == 0:
            # 正在建索引 / 尚未建索引：同样是"未就绪"，必须算进 not_ready。
            # 漏掉这一档会让界面在后台索引还没跑完时误报"全部就绪"。
            pending_ids.append(s.doc_id)

    # 从未被索引过的文档（识别成功但没有索引状态记录）
    never_indexed = sorted(valid_ids - {s.doc_id for s in state_rows})

    # 界面提示与启动补偿共用：当前版本尚无有效索引的全部文档
    not_ready = sorted(
        set(never_indexed) | set(stale_ids) | set(pending_ids)
        | {d["doc_id"] for d in failed}
    )

    # 向量可用性以"库中是否存在带向量的分块"为准，
    # 而不是缓存是否已加载（缓存是懒加载的，未检索过时尚未加载）
    vector_ready = embedded_chunks > 0
    model_tag = None
    if vector_ready:
        # 用 first() 而非 scalar()：多个分块都有 embed_model，scalar() 会因多行报错
        row = db.query(DocumentChunk.embed_model).filter(
            DocumentChunk.embedding.isnot(None)
        ).first()
        model_tag = row[0] if row else None

    return {
        "total_docs": total_docs,               # 未在回收站且识别成功的文档数
        "indexed_docs": len(indexed_ids),       # 当前版本索引已就绪数
        "pending_docs": by_status.get(0, 0),    # 待索引数
        "failed_docs": by_status.get(2, 0),     # 索引失败数
        "stale_docs": sorted(stale_ids),        # 索引成功但版本已过期（不用于兜底）
        "never_indexed": never_indexed,         # 从未触发过索引的文档ID
        "not_ready": not_ready,                 # 当前版本无有效索引的全部文档ID
        "all_ready": not not_ready,             # 是否全部就绪（界面据此隐藏提示条）
        "total_chunks": total_chunks,
        "embedded_chunks": embedded_chunks,
        "vector_ready": vector_ready,
        "model_tag": model_tag,
        "cache_loaded": _cache.is_loaded,
        "failed_detail": failed,
    }
