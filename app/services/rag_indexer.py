# -*- coding: utf-8 -*-
"""
RAG 索引编排模块

职责：把「取有效快照 → 语义分块 → 向量化 → 单事务原子替换 → 缓存失效」串成索引链路。

与 rag_pipeline 的分工：
  rag_indexer  —— 索引期（离线，文档入库/版本发布后跑）
  rag_pipeline —— 查询期（在线，用户提问时跑）

方案 B（任务书 3.2）改造要点：
  1. **索引的是当前有效版本**，不是原始 OCR 文本。快照里的 effective_text
     是人工编辑后的正文；fields 还要单独生成一条带标签的结构化分块
     （source_kind=fields），让"金额/日期"这类问题也能被精准召回。
  2. **原子替换**：删除旧分块、写新分块、更新索引状态在同一个事务里提交一次。
     旧实现的 delete→commit→insert→commit 中间存在"已删未建"窗口，检索会漏召回。
  3. **事务内二次校验**：生成分块期间用户可能已删除文档或发布新版本，
     提交前再次比对 expected_revision，不一致就整事务回滚（见 replace_chunks_atomic）。
  4. **独立 Session**：后台索引（BackgroundTasks / 启动补偿）自带会话，
     不与请求会话共享，避免请求提交/回滚干扰索引事务。
  5. **启动补偿**：只补偿"当前有效、未删除、索引缺失/过期/失败"的文档，
     **不重新 OCR**（任务书 3.2）。
  6. 索引未就绪的文档不用旧索引兜底 —— 它们根本不进候选集，界面另行提示。
"""
import logging
import threading
import time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DocumentInfo
from app.services import revisions as rev_svc
from app.services.chunker import chunk_text
from app.services.embedding import get_embedding_model
from app.services.bm25_index import (
    get_bm25_index, rebuild_bm25_index, invalidate_index, rebuild_lock,
)
from app.services.vector_store import (
    replace_chunks_atomic, set_index_state, load_chunks_for_bm25, get_index_status,
)
from app.services.backup_gate import GATE, BackupInProgress

logger = logging.getLogger(__name__)

# 索引状态码（与 document_info.status 语义保持一致）
STATUS_PENDING = 0
STATUS_SUCCESS = 1
STATUS_FAILED = 2

# 文档类型中文标签（用于结构化字段分块的标题行）
DOC_TYPE_LABELS = {"invoice": "发票", "contract": "合同", "other": "文档"}

# 启动补偿最多处理的文档数 —— 防止异常情况下一启动就把 CPU 打满
MAX_COMPENSATE_DOCS = 500


# ==================== BM25 内存索引 ====================

def rebuild_bm25(db: Session) -> int:
    """从数据库全量重建 BM25 内存索引，返回索引的分块数"""
    chunks = load_chunks_for_bm25(db)
    rebuild_bm25_index(chunks)
    return len(chunks)


def ensure_bm25_ready(db: Session) -> bool:
    """
    确保 BM25 索引可用；未就绪时自动重建。

    检索路径调用它，实现"按需重建"而非每次写入都全量重建。

    并发安全：外层先做一次无锁快检（绝大多数请求走这条路径），
    未就绪才去抢 `rebuild_lock`，并在锁内**再查一次** ——
    等锁期间索引可能已被别的线程重建好，此时直接复用，不重复重建。
    重建 = 全量读库 + 分词，是很贵的操作，N 个并发请求各做一遍不可接受。
    """
    if get_bm25_index().is_ready:
        return True

    with rebuild_lock:
        if get_bm25_index().is_ready:        # 双重检查
            return True
        n = rebuild_bm25(db)
        logger.info(f"BM25 索引按需重建完成：{n} 个分块")

    return n > 0


# ==================== 结构化字段分块 ====================

def build_fields_chunk(doc_type: str, fields: List[Dict]) -> Optional[str]:
    """
    把当前有效字段拼成一条**有标签的结构化分块**。

    为什么正文之外还要这一条：字段名本身就是强先验。
    "合计金额 80.00" 这种表述在正文里可能被拆到不同分块里，
    而查询词常常只有"金额是多少"。单独一条带中文标签的来源，
    能让标签词与值同处一个检索单元，命中率显著高于靠正文碰运气。

    返回 None 表示该文档没有可展示的有效字段，不必建这条分块。
    """
    lines = [f"【{DOC_TYPE_LABELS.get(doc_type, '文档')}字段】"]
    for f in fields:
        name = (f.get("field_name") or "").strip()
        value = f.get("field_value")
        if not name or value in (None, ""):
            continue
        lines.append(f"{name}：{value}")
    if len(lines) == 1:
        return None
    return "\n".join(lines)


# ==================== 单文档索引 ====================

@GATE.writer
def index_document(db: Session, doc_id: int) -> Dict:
    """
    对单篇文档执行完整索引链路（索引**当前有效版本**）。

    返回: {"doc_id":.., "ok":bool, "chunk_count":int, "vectorized":bool,
           "cost_ms":float, "error":str|None, "reason":str|None}
          reason: deleted / version_changed / no_revision / no_snapshot /
                  no_text / no_chunks / exception / backup_in_progress
    """
    t0 = time.time()
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()

    if doc is None:
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": 0.0, "error": "文档不存在", "reason": "document_missing"}

    if doc.deleted_at is not None:
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": 0.0, "error": "文档已在回收站，跳过索引", "reason": "deleted"}

    if doc.current_revision is None:
        msg = "尚无有效版本（首次识别未成功），跳过索引"
        set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": 0.0, "error": msg, "reason": "no_revision"}

    snap = rev_svc.get_effective_snapshot(db, doc_id)
    if snap is None:
        msg = f"当前版本 r{doc.current_revision} 无对应快照，无法建立索引"
        set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
        logger.warning(f"[索引] 文档 {doc_id}: {msg}")
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": 0.0, "error": msg, "reason": "no_snapshot"}

    text = (snap.effective_text or "").strip()
    if not text:
        msg = "当前版本的有效全文为空，无法建立索引"
        set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": 0.0, "error": msg, "reason": "no_text"}

    expected_revision = doc.current_revision
    set_index_state(db, doc_id, STATUS_PENDING)

    try:
        # ① 语义聚合式分块（内部已含归一化）—— 来源为正文
        chunks: List[Dict] = []
        for c in chunk_text(text):
            c["source_kind"] = "text"
            chunks.append(c)
        for i, c in enumerate(chunks):
            c["chunk_index"] = i

        if not chunks:
            msg = "分块结果为空"
            set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
            return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                    "cost_ms": round((time.time() - t0) * 1000, 1), "error": msg,
                    "reason": "no_chunks"}

        # ② 结构化字段分块 —— 一条带标签来源（source_kind=fields）
        fields = rev_svc.snapshot_fields(snap)
        fields_text = build_fields_chunk(snap.doc_type, fields)
        if fields_text:
            chunks.append({
                "chunk_index": len(chunks),
                "chunk_text": fields_text,
                "chunk_text_raw": fields_text,
                "char_start": None,   # 字段不与正文同一坐标系，不提供偏移
                "char_end": None,
                "page_no": None,
                "source_kind": "fields",
            })

        # ③ 向量化（模型不可用时降级：仍写文本，BM25 单路可用）
        model = get_embedding_model()
        embeddings = None
        model_tag = "unavailable"
        if model.is_ready or model.load():
            embeddings = model.encode([c["chunk_text"] for c in chunks], is_query=False)
            model_tag = model.model_tag
        else:
            logger.warning(f"[索引] 文档 {doc_id}: 向量模型不可用，仅写入分块文本（BM25 单路可用）")

        # 备份期间走到这一步：由于替换是整体事务提交的，这里选择"中止"
        # 而不是"带着可能与备份快照不一致的数据继续写"
        GATE.abort_if_closed(f"索引替换 doc={doc_id}")

        # ④ 单事务原子替换（内含版本/删除状态二次校验）
        res = replace_chunks_atomic(
            db, doc_id, chunks, embeddings, model_tag, revision=expected_revision
        )
        if not res["ok"]:
            reason = res["reason"]
            msg = {
                "deleted": "索引期间文档被移入回收站，已中止",
                "version_changed": "索引期间文档已发布新版本，本次结果作废待重跑",
                "document_missing": "文档已不存在",
                "exception": "原子替换失败已回滚",
            }.get(reason, "索引替换失败")
            if reason in ("deleted", "version_changed"):
                # 不是错误，只是被更新的事实追上了；状态留给后续补偿/重试处理
                logger.info(f"[索引] 文档 {doc_id} {msg}")
            else:
                set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
            return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                    "cost_ms": round((time.time() - t0) * 1000, 1), "error": msg,
                    "reason": reason}

        # ⑤ BM25 索引标记失效，下次检索按需重建
        #    用 invalidate_index()（会递增代数）而不是直接操作实例 ——
        #    若有重建正在构建中，代数的变化会让它把自己标为待重建，避免漏掉本次写入
        invalidate_index()

        cost = (time.time() - t0) * 1000
        logger.info(f"[索引] 文档 {doc_id} r{expected_revision} 完成：{res['chunk_count']} 个分块"
                    f"（字段来源 {1 if fields_text else 0} 条），"
                    f"向量化={'是' if embeddings is not None else '否(降级)'}，耗时 {cost:.0f}ms")
        return {"doc_id": doc_id, "ok": True, "chunk_count": res["chunk_count"],
                "vectorized": embeddings is not None, "cost_ms": round(cost, 1),
                "error": None, "reason": None}

    except BackupInProgress as e:
        # 备份窗口：任务中止，状态保持 pending，等待补偿/手动重试
        db.rollback()
        logger.warning(f"[索引] 文档 {doc_id} 因备份中止: {e}")
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": round((time.time() - t0) * 1000, 1),
                "error": str(e), "reason": "backup_in_progress"}

    except Exception as e:
        db.rollback()
        logger.error(f"[索引] 文档 {doc_id} 失败: {e}", exc_info=True)
        set_index_state(db, doc_id, STATUS_FAILED, error_msg=str(e)[:500])
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": round((time.time() - t0) * 1000, 1),
                "error": str(e), "reason": "exception"}


# ==================== 后台索引（独立 Session） ====================

def index_document_background(doc_id: int) -> Dict:
    """
    BackgroundTasks / 启动补偿调用的后台入口。

    **必须自带 Session**：请求会话在响应返回后就关闭了，
    复用它会命中 "Session is closed"，且请求自己的提交/回滚会干扰索引事务。
    """
    db = SessionLocal()
    try:
        return index_document(db, doc_id)
    except Exception as e:
        logger.error(f"[索引] 后台任务异常 doc={doc_id}: {e}", exc_info=True)
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": 0.0, "error": str(e)[:500], "reason": "exception"}
    finally:
        db.close()


# ==================== 批量与补偿 ====================

def _candidate_doc_ids(db: Session, doc_ids: Optional[List[int]]) -> List[int]:
    """待索引候选集：默认取全部未被删除的文档"""
    q = db.query(DocumentInfo.id).filter(DocumentInfo.deleted_at.is_(None))
    if doc_ids is not None:
        if len(doc_ids) == 0:
            return []
        q = q.filter(DocumentInfo.id.in_(doc_ids))
    return [row[0] for row in q.all()]


def index_documents(db: Session, doc_ids: Optional[List[int]] = None) -> Dict:
    """
    批量重建索引。

    参数: doc_ids — 指定文档ID列表；为 None 时对**所有未删除**的文档重建
    返回: {"total":.., "success":.., "failed":.., "chunks":.., "cost_ms":.., "details":[..]}
    """
    t0 = time.time()

    targets = _candidate_doc_ids(db, doc_ids)
    total = len(targets)
    success = 0
    failed = 0
    chunks_total = 0
    details: List[Dict] = []

    for i, doc_id in enumerate(targets, 1):
        # 批量任务的阶段边界：备份开始时中止剩余部分，已索引的保留
        try:
            GATE.abort_if_closed(f"批量索引 {i}/{total}")
        except BackupInProgress as e:
            logger.warning(f"[索引] 批量任务在第 {i}/{total} 篇中止: {e}")
            details.append({"doc_id": doc_id, "ok": False, "chunk_count": 0,
                            "error": str(e), "reason": "backup_in_progress"})
            failed += total - i + 1
            break

        r = index_document(db, doc_id)
        details.append(r)
        if r["ok"]:
            success += 1
            chunks_total += r["chunk_count"]
        else:
            failed += 1

    # 批量重建后强制重建 BM25 索引
    n = rebuild_bm25(db)

    cost = (time.time() - t0) * 1000
    logger.info(f"[索引] 批量重建完成：{success}/{total} 成功，"
                f"{chunks_total} 个分块，BM25 索引 {n} 条，总耗时 {cost:.0f}ms")

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "chunks": chunks_total,
        "bm25_chunks": n,
        "cost_ms": round(cost, 1),
        "details": details,
    }


def find_compensation_targets(db: Session, limit: int = MAX_COMPENSATE_DOCS) -> List[int]:
    """
    启动补偿目标：当前有效、未删除，且索引缺失/过期/失败的文档。

    与旧 `never_indexed` 的区别：把"索引成功但版本已漂移"也算进来 ——
    上次进程可能是在索引途中断的，此时索引指向旧版本而没有后续线程纠正。
    """
    status = get_index_status(db)
    return sorted(set(status.get("not_ready") or []))[:limit]


def reindex_never_indexed(db: Session) -> Dict:
    """手动重试入口（/rag/index/retry）：补齐所有未就绪文档的索引"""
    targets = find_compensation_targets(db)
    if not targets:
        return {"total": 0, "success": 0, "failed": 0, "chunks": 0,
                "bm25_chunks": get_bm25_index().doc_count, "cost_ms": 0.0, "details": []}
    return index_documents(db, targets)


def compensate_on_startup(delay: float = 3.0) -> Optional[threading.Thread]:
    """
    启动补偿（任务书 3.2）：以守护线程补建索引，**不重新 OCR**。

    为什么延迟几秒：启动阶段还在预加载 OCR / 向量模型，
    立刻跑 embedding 会与模型加载抢 CPU，也会撞上模型尚未就绪的时间窗。
    """
    def _run():
        time.sleep(delay)
        db = SessionLocal()
        try:
            targets = find_compensation_targets(db)
        except Exception as e:
            logger.error(f"[索引] 启动补偿查询失败: {e}", exc_info=True)
            return
        finally:
            db.close()

        if not targets:
            logger.info("[索引] 启动补偿：无待补偿文档")
            return

        logger.info(f"[索引] 启动补偿：{len(targets)} 篇待建索引 -> {targets}")
        db = SessionLocal()
        try:
            r = index_documents(db, targets)
            logger.info(
                f"[索引] 启动补偿完成：{r['success']}/{r['total']} 成功，"
                f"{r['chunks']} 个分块，耗时 {r['cost_ms']:.0f}ms"
            )
        except Exception as e:
            logger.error(f"[索引] 启动补偿异常: {e}", exc_info=True)
        finally:
            db.close()

    t = threading.Thread(target=_run, name="index-compensation", daemon=True)
    t.start()
    return t
