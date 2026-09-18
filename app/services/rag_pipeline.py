"""
RAG 查询链路编排模块

阶段二实现「检索段」：双路检索 → 融合 → 去重 → 阈值截断
阶段三将补上「生成段」：查询改写 → 组装 Prompt → LLM 生成 → 引用溯源

与 rag_indexer 的分工：
  rag_indexer  —— 索引期（离线，文档入库时跑）
  rag_pipeline —— 查询期（在线，用户提问时跑）
"""
import hashlib
import logging
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import (
    RETRIEVE_TOP_N_VEC, RETRIEVE_TOP_N_BM25, RETRIEVE_TOP_K,
    RETRIEVE_MIN_SCORE, FUSION_MODE,
)
from app.services.bm25_index import get_bm25_index
from app.services.embedding import get_embedding_model
from app.services.fusion import fuse
from app.services.rag_indexer import ensure_bm25_ready
from app.services.vector_store import search_vector

logger = logging.getLogger(__name__)

# 每个文档最多保留的引用条数 —— 避免同一文档占满 Top-K 名额
MAX_HITS_PER_DOC = 2

# 引用片段的最大展示长度
SNIPPET_MAX_LEN = 200


def _snippet(text: str, limit: int = SNIPPET_MAX_LEN) -> str:
    """截取引用片段用于前端展示"""
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _dedup(hits: List[Dict], max_per_doc: int = MAX_HITS_PER_DOC) -> List[Dict]:
    """
    去重与合并。

    解决两个真实问题：
      1. 语料中存在**内容完全相同**的文档（实测 ID3 与 ID4 一字不差），
         检索会返回重复引用，白白占用 Top-K 名额 → 按内容哈希去重
      2. 同一文档的多个分块同时命中 → 限制每个文档的引用条数，
         让 Top-K 名额分配给更多不同文档
    """
    seen_hash = set()
    per_doc_count: Dict[int, int] = {}
    result: List[Dict] = []

    for h in hits:
        text = h.get("chunk_text") or ""
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()
        if digest in seen_hash:
            continue

        doc_id = h.get("doc_id")
        if per_doc_count.get(doc_id, 0) >= max_per_doc:
            continue

        seen_hash.add(digest)
        per_doc_count[doc_id] = per_doc_count.get(doc_id, 0) + 1
        result.append(h)

    return result


def retrieve(db: Session, query: str, mode: str = "hybrid",
             top_k: Optional[int] = None,
             doc_ids: Optional[List[int]] = None,
             min_score: Optional[float] = None,
             fusion_mode: Optional[str] = None) -> Dict:
    """
    检索入口：双路并行检索 → 分数融合 → 去重 → 阈值截断。

    参数:
      mode        — "vec" 仅向量 / "bm25" 仅关键词 / "hybrid" 双路融合
      top_k       — 最终返回条数，默认取配置 RETRIEVE_TOP_K
      doc_ids     — 限定检索的文档范围
      min_score   — 向量相似度阈值，默认取配置 RETRIEVE_MIN_SCORE
      fusion_mode — 融合策略 "rrf" / "weighted"，默认取配置 FUSION_MODE

    返回:
      {
        "hits": [...],           # 去重截断后的结果，含 vec_rank / bm25_rank
        "mode": "hybrid",
        "fusion_mode": "rrf",
        "vec_count": 12,         # 向量路原始召回数
        "bm25_count": 7,         # 关键词路原始召回数
        "degraded": False,       # 是否因模型不可用而降级
        "degrade_reason": None,
      }
    """
    top_k = top_k or RETRIEVE_TOP_K
    min_score = RETRIEVE_MIN_SCORE if min_score is None else min_score
    fusion_mode = fusion_mode or FUSION_MODE

    vec_hits: List[Dict] = []
    bm25_hits: List[Dict] = []
    degraded = False
    degrade_reason = None

    # ---------- 语义路 ----------
    if mode in ("vec", "hybrid"):
        model = get_embedding_model()
        if model.is_ready or model.load():
            qv = model.encode_one(query, is_query=True)
            if qv is not None:
                vec_hits = search_vector(db, qv, top_n=RETRIEVE_TOP_N_VEC, doc_ids=doc_ids)
                # 阈值截断：低于阈值的向量命中在融合前丢弃
                # （bge-zh 相似度区间较窄，阈值需按语料实测调整，见 config.py 说明）
                before = len(vec_hits)
                vec_hits = [h for h in vec_hits if h["score"] >= min_score]
                if before != len(vec_hits):
                    logger.info(f"向量路阈值截断({min_score}): {before} → {len(vec_hits)} 条")
        else:
            degraded = True
            degrade_reason = "vector_model_unavailable"
            logger.warning("向量模型不可用，本次检索降级为 BM25 单路")

    # ---------- 关键词路 ----------
    if mode in ("bm25", "hybrid"):
        if ensure_bm25_ready(db):
            bm25_hits = get_bm25_index().search(query, top_n=RETRIEVE_TOP_N_BM25)

    # ---------- 融合 ----------
    if mode == "vec":
        fused = vec_hits
    elif mode == "bm25":
        fused = bm25_hits
    else:
        if not vec_hits and not bm25_hits:
            fused = []
        else:
            fused = fuse(vec_hits, bm25_hits, mode=fusion_mode)

    # ---------- 去重与截断 ----------
    final = _dedup(fused)[:top_k]

    # ---------- 补充展示字段 ----------
    for h in final:
        h["snippet"] = _snippet(h.get("chunk_text_raw") or h.get("chunk_text") or "")

    return {
        "hits": final,
        "mode": mode,
        "fusion_mode": fusion_mode if mode == "hybrid" else None,
        "vec_count": len(vec_hits),
        "bm25_count": len(bm25_hits),
        "min_score": min_score,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
    }
