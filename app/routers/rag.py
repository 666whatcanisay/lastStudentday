"""
RAG 检索增强生成路由

  POST /api/rag/index          重建索引（指定文档或全量）
  GET  /api/rag/index/status   索引状态（已索引/待索引/失败明细）
  POST /api/rag/index/retry    重试未索引与索引失败的文档
  POST /api/rag/search         检索（支持 vec / bm25 / hybrid 三种模式）
  POST /api/rag/ask            问答（查询改写 + 双路检索 + LLM 生成 + 引用溯源）
  GET  /api/rag/config         当前 RAG 配置与可用性
  POST /api/rag/rewrite        仅做查询改写，返回改写详情（调试用，不检索）
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import (
    RAG_ENABLED, EMBED_MODEL_NAME, EMBED_DIM, CHUNK_SIZE, CHUNK_OVERLAP,
    RETRIEVE_TOP_N_VEC, RETRIEVE_TOP_N_BM25, RETRIEVE_TOP_K, RETRIEVE_MIN_SCORE,
    RETRIEVE_SCORE_MARGIN, RETRIEVE_NO_HIT_SCORE,
    FUSION_MODE, RRF_K, BM25_K1, BM25_B, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL,
    QUERY_REWRITE_ENABLE, MAX_CONTEXT_CHARS, LLM_USE_ENV_PROXY, LLM_TIMEOUT,
    LLM_TEMPERATURE,
)
from app.database import get_db
from app.schemas import ApiResponse
from app.services.embedding import get_embedding_model
from app.services.llm_client import get_llm_client
from app.services.query_rewrite import rewrite as rewrite_query
from app.services.rag_indexer import index_documents, reindex_never_indexed
from app.services.rag_pipeline import ask as rag_ask, retrieve
from app.services.vector_store import get_index_status

router = APIRouter(prefix="/api/rag", tags=["RAG检索增强"])
logger = logging.getLogger(__name__)


# ==================== 请求模型 ====================

class RagIndexRequest(BaseModel):
    """索引重建请求"""
    doc_ids: Optional[List[int]] = Field(
        default=None, description="指定文档ID列表；不传则对所有识别成功的文档全量重建")


class RagSearchRequest(BaseModel):
    """检索请求"""
    query: str = Field(..., min_length=1, description="查询文本")
    mode: str = Field(default="hybrid", description="检索模式：vec 仅向量 / bm25 仅关键词 / hybrid 双路融合")
    top_k: Optional[int] = Field(default=None, ge=1, le=50, description="返回条数")
    doc_ids: Optional[List[int]] = Field(default=None, description="限定检索的文档ID范围")
    min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="向量相似度阈值")
    fusion_mode: Optional[str] = Field(default=None, description="融合策略：rrf / weighted")
    rewrite: bool = Field(
        default=True,
        description="是否启用查询改写（相对时间解析 + 同义扩展）。"
                    "默认开启，与 /ask 行为一致；设为 false 可观察『未改写』的原始检索效果，用于对比实验")


class RagAskRequest(BaseModel):
    """问答请求"""
    question: str = Field(..., min_length=1, description="用户问题（可口语化、可含相对时间）")
    mode: str = Field(default="hybrid", description="检索模式：vec / bm25 / hybrid")
    top_k: Optional[int] = Field(default=None, ge=1, le=20, description="送入 Prompt 的资料条数")
    doc_ids: Optional[List[int]] = Field(default=None, description="限定检索的文档ID范围")
    min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="向量相似度阈值")
    fusion_mode: Optional[str] = Field(default=None, description="融合策略：rrf / weighted")
    rewrite: bool = Field(default=True, description="是否启用查询改写（相对时间解析 + 同义扩展）")
    use_llm_rewrite: bool = Field(default=False, description="是否额外用 LLM 改写查询（会增加一次调用）")


class RagRewriteRequest(BaseModel):
    """查询改写调试请求"""
    query: str = Field(..., min_length=1, description="待改写的查询")
    use_llm_rewrite: bool = Field(default=False, description="是否用 LLM 改写")


# ==================== 索引管理 ====================

@router.post("/index", response_model=ApiResponse)
async def rebuild_index(req: RagIndexRequest, db: Session = Depends(get_db)):
    """
    重建 RAG 索引接口

    流程：归一化 → 语义分块 → 向量化 → 写入 document_chunk → 重建 BM25 索引
    幂等：重复调用不会产生重复分块（内部先删后建）
    """
    if not RAG_ENABLED:
        return ApiResponse.error(msg="RAG 功能未启用（RAG_ENABLED=False）", code=503)

    logger.info(f"=== 开始重建索引 doc_ids={req.doc_ids or '全部'} ===")
    result = index_documents(db, req.doc_ids)
    return ApiResponse.success(data=result, msg=f"索引重建完成：{result['success']}/{result['total']} 成功")


@router.get("/index/status", response_model=ApiResponse)
async def index_status(db: Session = Depends(get_db)):
    """获取索引状态：已索引/待索引/失败文档数、分块数、向量就绪情况"""
    status = get_index_status(db)
    return ApiResponse.success(data=status)


@router.post("/index/retry", response_model=ApiResponse)
async def retry_index(db: Session = Depends(get_db)):
    """
    重试索引接口

    对「从未索引过」和「索引失败」的文档补建索引。
    解决 BackgroundTasks 异步索引在进程重启后丢失任务、导致部分文档永久未索引的问题。
    """
    if not RAG_ENABLED:
        return ApiResponse.error(msg="RAG 功能未启用", code=503)

    result = reindex_never_indexed(db)
    if result["total"] == 0:
        return ApiResponse.success(data=result, msg="没有需要重试的文档")
    return ApiResponse.success(data=result, msg=f"重试完成：{result['success']}/{result['total']} 成功")


# ==================== 检索 ====================

@router.post("/search", response_model=ApiResponse)
async def search(req: RagSearchRequest, db: Session = Depends(get_db)):
    """
    检索接口（不调用 LLM，纯检索）

    用途：
      1. 前端「检索模式切换」对比 —— 同一查询分别用 vec / bm25 / hybrid 看效果差异
      2. 调试与评估
      3. 未配置 LLM Key 时的降级出口

    查询改写默认开启，与 /ask 保持一致；置 rewrite=false 可对比「改写前 vs 改写后」，
    这也是 scripts/measure_rewrite.py 的分路实证所依赖的开关。

    每条结果回传 vec_rank 与 bm25_rank，便于观察"这条是哪一路召回的"。
    """
    if not RAG_ENABLED:
        return ApiResponse.error(msg="RAG 功能未启用", code=503)

    if req.mode not in ("vec", "bm25", "hybrid"):
        return ApiResponse.error(msg=f"不支持的检索模式: {req.mode}（可选 vec / bm25 / hybrid）", code=400)

    if req.rewrite:
        rw = rewrite_query(req.query, enable=True)
        vec_query, bm25_query = rw["vec_query"], rw["bm25_query"]
    else:
        rw = None
        vec_query = bm25_query = req.query

    result = retrieve(
        db,
        query=req.query,
        mode=req.mode,
        top_k=req.top_k,
        doc_ids=req.doc_ids,
        min_score=req.min_score,
        fusion_mode=req.fusion_mode,
        vec_query=vec_query,
        bm25_query=bm25_query,
    )
    result["rewrite"] = rw
    return ApiResponse.success(data=result)


# ==================== 问答 ====================

@router.post("/ask", response_model=ApiResponse)
async def ask(req: RagAskRequest, db: Session = Depends(get_db)):
    """
    智能问答接口（RAG 完整链路）

    链路：查询改写 → 双路检索 → RRF 融合 → 去重裁剪
          → 防幻觉 Prompt → LLM 生成 → 引用溯源

    降级设计（任何一环失效都不会 500）：
      · 向量模型不可用 → 自动降级为 BM25 单路，retrieval.degraded=True
      · 检索无结果     → 直接返回提示语，不浪费 LLM 调用
      · LLM 未配置/失败 → answer 置 null，返回 answer_text 提示 + 原文片段引用

    references 中每条含：
      · index      — 对应回答里的 [n] 编号
      · cited      — LLM 是否真的引用了它（前端可淡化未引用的条目）
      · vec_rank / bm25_rank — 双路溯源，None 表示该路未召回
      · char_start / char_end / page_no — 定位信息，供前端高亮原文
    """
    if not RAG_ENABLED:
        return ApiResponse.error(msg="RAG 功能未启用", code=503)

    if req.mode not in ("vec", "bm25", "hybrid"):
        return ApiResponse.error(msg=f"不支持的检索模式: {req.mode}（可选 vec / bm25 / hybrid）", code=400)

    if not req.question.strip():
        return ApiResponse.error(msg="问题不能为空", code=400)

    result = rag_ask(
        db,
        question=req.question,
        mode=req.mode,
        top_k=req.top_k,
        doc_ids=req.doc_ids,
        min_score=req.min_score,
        fusion_mode=req.fusion_mode,
        rewrite_enable=req.rewrite,
        use_llm_rewrite=req.use_llm_rewrite,
    )

    # 降级时给前端一个明确的 msg，便于 UI 提示
    if result.get("degraded"):
        reason = result.get("degrade_reason")
        msg_map = {
            "no_hits": "未检索到相关资料",
            "llm_not_configured": "未配置 LLM，已降级为仅检索模式",
            "llm_call_failed": "LLM 调用失败，已降级为仅检索模式",
            "vector_model_unavailable": "向量模型不可用，已降级为关键词检索",
        }
        return ApiResponse.success(data=result, msg=msg_map.get(reason, "已降级"))

    return ApiResponse.success(data=result)


@router.post("/rewrite", response_model=ApiResponse)
async def rewrite_only(req: RagRewriteRequest):
    """
    仅执行查询改写，不做检索（调试与教学演示用）

    便于直观看到「上个月」被解析成了哪个绝对区间、口语词被扩展成了哪些术语。
    """
    if not RAG_ENABLED:
        return ApiResponse.error(msg="RAG 功能未启用", code=503)

    result = rewrite_query(
        req.query,
        enable=True,
        use_llm=req.use_llm_rewrite,
        llm_client=get_llm_client() if req.use_llm_rewrite else None,
    )
    return ApiResponse.success(data=result)


# ==================== 配置与可用性 ====================

@router.get("/config", response_model=ApiResponse)
async def get_rag_config():
    """
    获取 RAG 配置与可用性状态

    前端据此判断：
      - 向量模型是否就绪（否则提示降级）
      - LLM 是否已配置（否则问答走降级路径）
    """
    model = get_embedding_model()
    info = model.get_info()

    # 只暴露"是否已配置"，绝不回传 Key 本身
    llm_configured = bool(LLM_API_KEY)

    return ApiResponse.success(data={
        "enabled": RAG_ENABLED,
        "embedding": {
            "model_name": EMBED_MODEL_NAME,
            "dim": EMBED_DIM,
            "ready": info["ready"],
            "quantized": info["quantized"],
            "error": info["error"],
        },
        "llm": {
            "configured": llm_configured,
            "model": LLM_MODEL,
            "base_url": LLM_BASE_URL,
            "use_env_proxy": LLM_USE_ENV_PROXY,
            "timeout": LLM_TIMEOUT,
            "temperature": LLM_TEMPERATURE,
            # 未配置 Key 时问答会走降级路径，前端据此提示用户
            "degrade_hint": None if llm_configured else "未配置 LLM_API_KEY，问答将降级为仅检索模式",
        },
        "retrieval": {
            "top_n_vec": RETRIEVE_TOP_N_VEC,
            "top_n_bm25": RETRIEVE_TOP_N_BM25,
            "top_k": RETRIEVE_TOP_K,
            "min_score": RETRIEVE_MIN_SCORE,
            "score_margin": RETRIEVE_SCORE_MARGIN,
            "no_hit_score": RETRIEVE_NO_HIT_SCORE,
            "fusion_mode": FUSION_MODE,
            "rrf_k": RRF_K,
            "bm25_k1": BM25_K1,
            "bm25_b": BM25_B,
        },
        "chunking": {
            "size": CHUNK_SIZE,
            "overlap": CHUNK_OVERLAP,
        },
        "query_rewrite": {
            "enabled": QUERY_REWRITE_ENABLE,
        },
        "max_context_chars": MAX_CONTEXT_CHARS,
    })
