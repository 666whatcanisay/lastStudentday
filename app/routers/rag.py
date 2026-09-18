"""
RAG 检索增强生成路由

  POST /api/rag/index          重建索引（指定文档或全量）
  GET  /api/rag/index/status   索引状态（已索引/待索引/失败明细）
  POST /api/rag/index/retry    重试未索引与索引失败的文档
  POST /api/rag/search         检索（支持 vec / bm25 / hybrid 三种模式）
  GET  /api/rag/config         当前 RAG 配置与可用性

阶段三将补充：POST /api/rag/ask（问答，含 LLM 生成与引用溯源）
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import (
    RAG_ENABLED, EMBED_MODEL_NAME, EMBED_DIM, CHUNK_SIZE, CHUNK_OVERLAP,
    RETRIEVE_TOP_N_VEC, RETRIEVE_TOP_N_BM25, RETRIEVE_TOP_K, RETRIEVE_MIN_SCORE,
    FUSION_MODE, RRF_K, BM25_K1, BM25_B, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL,
    QUERY_REWRITE_ENABLE, MAX_CONTEXT_CHARS,
)
from app.database import get_db
from app.schemas import ApiResponse
from app.services.embedding import get_embedding_model
from app.services.rag_indexer import index_documents, reindex_never_indexed
from app.services.rag_pipeline import retrieve
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

    每条结果回传 vec_rank 与 bm25_rank，便于观察"这条是哪一路召回的"。
    """
    if not RAG_ENABLED:
        return ApiResponse.error(msg="RAG 功能未启用", code=503)

    if req.mode not in ("vec", "bm25", "hybrid"):
        return ApiResponse.error(msg=f"不支持的检索模式: {req.mode}（可选 vec / bm25 / hybrid）", code=400)

    result = retrieve(
        db,
        query=req.query,
        mode=req.mode,
        top_k=req.top_k,
        doc_ids=req.doc_ids,
        min_score=req.min_score,
        fusion_mode=req.fusion_mode,
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
        },
        "retrieval": {
            "top_n_vec": RETRIEVE_TOP_N_VEC,
            "top_n_bm25": RETRIEVE_TOP_N_BM25,
            "top_k": RETRIEVE_TOP_K,
            "min_score": RETRIEVE_MIN_SCORE,
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
