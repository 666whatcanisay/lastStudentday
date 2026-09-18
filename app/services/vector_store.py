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

    def invalidate(self) -> None:
        """标记缓存失效（索引写入/删除后调用）"""
        self._dirty = True

    @property
    def is_loaded(self) -> bool:
        return not self._dirty and self._matrix is not None

    def get(self, db: Session) -> Tuple[Optional[np.ndarray], List[Dict]]:
        """获取向量矩阵与元信息，必要时重建"""
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

def delete_doc_chunks(db: Session, doc_id: int) -> int:
    """删除某文档的全部分块（重建索引前先调用，保证幂等）"""
    n = db.query(DocumentChunk).filter(DocumentChunk.doc_id == doc_id).delete()
    db.commit()
    if n:
        logger.info(f"已清除文档 {doc_id} 的 {n} 个旧分块")
    return n


def upsert_chunks(db: Session, doc_id: int, chunks: List[Dict],
                  embeddings: Optional[np.ndarray], model_tag: str) -> int:
    """
    写入文档分块（先删后建，保证幂等）。

    参数: chunks     — chunker 产出的分块列表
          embeddings — shape=(len(chunks), dim) 的归一化向量；None 表示仅存文本不存向量
          model_tag  — 向量模型标识
    返回: 写入的分块数
    """
    # 先清除旧分块 —— 解决 P1-5「重复建索引累积重复分块」
    delete_doc_chunks(db, doc_id)

    count = 0
    for i, ch in enumerate(chunks):
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
        ))
        count += 1

    db.commit()
    _cache.invalidate()
    logger.info(f"文档 {doc_id} 写入 {count} 个分块（含向量 {count if embeddings is not None else 0} 条）")
    return count


def set_index_state(db: Session, doc_id: int, status: int, chunk_count: Optional[int] = None,
                    embed_model: Optional[str] = None, error_msg: Optional[str] = None) -> None:
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
    state.error_msg = error_msg
    db.commit()


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
    if doc_ids:
        allowed = set(doc_ids)
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
        }
        for c, fn in rows
    ]


def get_index_status(db: Session) -> Dict:
    """
    索引状态统计，供 /api/rag/index/status 使用。

    能回答"哪些文档还没索引 / 索引失败"—— 这是 v1 方案缺失的可观测性。
    """
    total_docs = db.query(func.count(DocumentInfo.id)).filter(DocumentInfo.status == 1).scalar() or 0
    total_chunks = db.query(func.count(DocumentChunk.id)).scalar() or 0
    embedded_chunks = db.query(func.count(DocumentChunk.id)).filter(
        DocumentChunk.embedding.isnot(None)).scalar() or 0

    state_rows = db.query(DocIndexState).all()
    by_status = {0: 0, 1: 0, 2: 0}
    failed: List[Dict] = []
    indexed_ids = set()
    for s in state_rows:
        by_status[s.status] = by_status.get(s.status, 0) + 1
        if s.status == 1:
            indexed_ids.add(s.doc_id)
        elif s.status == 2:
            failed.append({"doc_id": s.doc_id, "error": s.error_msg})

    # 找出从未被索引过的文档（识别成功但没有索引状态记录）
    all_ok_ids = {row[0] for row in db.query(DocumentInfo.id).filter(DocumentInfo.status == 1).all()}
    never_indexed = sorted(all_ok_ids - indexed_ids)

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
        "total_docs": total_docs,               # 识别成功的文档数
        "indexed_docs": by_status.get(1, 0),    # 索引成功数
        "pending_docs": by_status.get(0, 0),    # 待索引数
        "failed_docs": by_status.get(2, 0),     # 索引失败数
        "never_indexed": never_indexed,         # 从未触发过索引的文档ID
        "total_chunks": total_chunks,
        "embedded_chunks": embedded_chunks,
        "vector_ready": vector_ready,
        "model_tag": model_tag,
        "cache_loaded": _cache.is_loaded,
        "failed_detail": failed,
    }
