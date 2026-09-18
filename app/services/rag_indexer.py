"""
RAG 索引编排模块

职责：把「归一化 → 语义分块 → 向量化 → 落库 → 更新状态」串成一条完整的索引链路。

与 rag_pipeline 的分工：
  rag_indexer  —— 索引期（文档入库时跑一次，离线）
  rag_pipeline —— 查询期（用户提问时跑，在线）

设计要点：
  1. 幂等：每次索引先清除该文档的旧分块（见 vector_store.upsert_chunks）
  2. 可观测：索引结果写入 doc_index_state 表，状态可查询、失败可重试
  3. 可降级：向量模型不可用时仍写入分块文本，BM25 单路依旧可用
  4. 异步触发：由 OCR 识别成功后经 BackgroundTasks 调用，不阻塞识别响应
"""
import logging
import time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import DocumentInfo
from app.services.chunker import chunk_text
from app.services.embedding import get_embedding_model
from app.services.bm25_index import get_bm25_index, rebuild_bm25_index
from app.services.vector_store import (
    upsert_chunks, set_index_state, load_chunks_for_bm25, get_index_status,
)

logger = logging.getLogger(__name__)

# 索引状态码（与 document_info.status 语义保持一致）
STATUS_PENDING = 0
STATUS_SUCCESS = 1
STATUS_FAILED = 2


def rebuild_bm25(db: Session) -> int:
    """从数据库全量重建 BM25 内存索引，返回索引的分块数"""
    chunks = load_chunks_for_bm25(db)
    rebuild_bm25_index(chunks)
    return len(chunks)


def ensure_bm25_ready(db: Session) -> bool:
    """
    确保 BM25 索引可用；未就绪时自动重建。

    检索路径调用它，实现"按需重建"而非每次写入都全量重建。
    """
    idx = get_bm25_index()
    if idx.is_ready:
        return True
    n = rebuild_bm25(db)
    logger.info(f"BM25 索引按需重建完成：{n} 个分块")
    return n > 0


def index_document(db: Session, doc_id: int) -> Dict:
    """
    对单篇文档执行完整索引链路。

    返回: {"doc_id":.., "ok":bool, "chunk_count":int, "vectorized":bool, "cost_ms":float, "error":str|None}
    """
    t0 = time.time()
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()

    if doc is None:
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0,
                "vectorized": False, "cost_ms": 0.0, "error": "文档不存在"}

    if not doc.ocr_raw_text or not doc.ocr_raw_text.strip():
        msg = "文档无 OCR 识别文本，无法建立索引"
        set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
        logger.warning(f"[索引] 文档 {doc_id}: {msg}")
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0,
                "vectorized": False, "cost_ms": 0.0, "error": msg}

    set_index_state(db, doc_id, STATUS_PENDING)

    try:
        # ① 语义聚合式分块（内部已含归一化）
        chunks = chunk_text(doc.ocr_raw_text)
        if not chunks:
            msg = "分块结果为空"
            set_index_state(db, doc_id, STATUS_FAILED, error_msg=msg)
            return {"doc_id": doc_id, "ok": False, "chunk_count": 0,
                    "vectorized": False, "cost_ms": (time.time() - t0) * 1000, "error": msg}

        # ② 向量化（模型不可用时降级：仍存文本，BM25 单路可用）
        model = get_embedding_model()
        embeddings = None
        model_tag = "unavailable"
        if model.is_ready or model.load():
            embeddings = model.encode([c["chunk_text"] for c in chunks], is_query=False)
            model_tag = model.model_tag
        else:
            logger.warning(f"[索引] 文档 {doc_id}: 向量模型不可用，仅写入分块文本（BM25 单路可用）")

        # ③ 落库（先删后建，幂等）
        n = upsert_chunks(db, doc_id, chunks, embeddings, model_tag)

        # ④ 更新索引状态
        set_index_state(db, doc_id, STATUS_SUCCESS, chunk_count=n,
                        embed_model=model_tag if embeddings is not None else None)

        # ⑤ BM25 索引标记失效，下次检索按需重建
        get_bm25_index().invalidate()

        cost = (time.time() - t0) * 1000
        logger.info(f"[索引] 文档 {doc_id} 完成：{n} 个分块，"
                    f"向量化={'是' if embeddings is not None else '否(降级)'}，耗时 {cost:.0f}ms")
        return {"doc_id": doc_id, "ok": True, "chunk_count": n,
                "vectorized": embeddings is not None, "cost_ms": round(cost, 1), "error": None}

    except Exception as e:
        logger.error(f"[索引] 文档 {doc_id} 失败: {e}", exc_info=True)
        set_index_state(db, doc_id, STATUS_FAILED, error_msg=str(e)[:500])
        return {"doc_id": doc_id, "ok": False, "chunk_count": 0, "vectorized": False,
                "cost_ms": round((time.time() - t0) * 1000, 1), "error": str(e)}


def index_documents(db: Session, doc_ids: Optional[List[int]] = None) -> Dict:
    """
    批量重建索引。

    参数: doc_ids — 指定文档ID列表；为 None 时对**所有识别成功的文档**全量重建
    返回: {"total":.., "success":.., "failed":.., "chunks":.., "cost_ms":.., "details":[..]}
    """
    t0 = time.time()

    if doc_ids:
        docs = db.query(DocumentInfo).filter(DocumentInfo.id.in_(doc_ids)).all()
    else:
        docs = db.query(DocumentInfo).filter(DocumentInfo.status == 1).all()

    total = len(docs)
    success = 0
    failed = 0
    chunks_total = 0
    details: List[Dict] = []

    for doc in docs:
        r = index_document(db, doc.id)
        details.append(r)
        if r["ok"]:
            success += 1
            chunks_total += r["chunk_count"]
        else:
            failed += 1

    # 全量重建后强制重建 BM25 索引
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


def reindex_never_indexed(db: Session) -> Dict:
    """对「从未索引过」的文档补建索引（进程重启后丢失的索引任务可由此补齐）"""
    status = get_index_status(db)
    pending_ids = status.get("never_indexed") or []
    failed_ids = [d["doc_id"] for d in status.get("failed_detail") or []]
    targets = sorted(set(pending_ids) | set(failed_ids))
    if not targets:
        return {"total": 0, "success": 0, "failed": 0, "chunks": 0,
                "bm25_chunks": get_bm25_index().doc_count, "cost_ms": 0.0, "details": []}
    return index_documents(db, targets)
