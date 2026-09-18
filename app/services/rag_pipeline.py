"""
RAG 查询链路编排模块

阶段二实现「检索段」：双路检索 → 融合 → 去重 → 阈值截断
阶段三补上「生成段」：查询改写 → 组装 Prompt → LLM 生成 → 引用溯源

与 rag_indexer 的分工：
  rag_indexer  —— 索引期（离线，文档入库时跑）
  rag_pipeline —— 查询期（在线，用户提问时跑）

两条降级链（任何一环失效都不阻断主流程）：
  1. 向量模型不可用 → 自动降级为 BM25 单路检索
  2. LLM 未配置 / 调用失败 → 直接返回检索原文片段，answer 置空并给出提示
"""
import hashlib
import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import (
    RETRIEVE_TOP_N_VEC, RETRIEVE_TOP_N_BM25, RETRIEVE_TOP_K,
    RETRIEVE_MIN_SCORE, FUSION_MODE, MAX_CONTEXT_CHARS, LLM_MAX_TOKENS,
)
from app.models import QaRecord
from app.services.bm25_index import get_bm25_index
from app.services.embedding import get_embedding_model
from app.services.fusion import fuse
from app.services.llm_client import get_llm_client
from app.services.query_rewrite import rewrite as rewrite_query
from app.services.rag_indexer import ensure_bm25_ready
from app.services.vector_store import search_vector

logger = logging.getLogger(__name__)

# 每个文档最多保留的引用条数 —— 避免同一文档占满 Top-K 名额
MAX_HITS_PER_DOC = 2

# 引用片段的最大展示长度
SNIPPET_MAX_LEN = 200

# 无答案时的标准话术（与 Prompt 中的规则 3 完全一致，便于前端判断）
NO_ANSWER_TEXT = "根据已有资料无法回答该问题。"

# 检索为空时的提示
NO_HIT_TEXT = "未检索到与问题相关的资料，请尝试换一种问法，或先对文档建立索引。"

# LLM 不可用时的降级提示
LLM_DOWN_TEXT = (
    "（当前未配置云端 LLM，已降级为「仅检索」模式。"
    "以下为与问题最相关的原文片段，请自行查阅；配置 LLM_API_KEY 后可获得归纳回答。）"
)


# ==================== 防幻觉 Prompt ====================

SYSTEM_PROMPT = """你是严谨的文档信息抽取助手。你的唯一信息来源是用户消息中提供的【资料片段】。

【硬性规则】
1. 只允许使用【资料片段】中出现的信息作答。禁止使用任何外部知识、常识推断或猜测。
2. 每一个事实性陈述后面必须紧跟来源编号标注，格式为 [1]、[2]（多个来源连写，如 [1][3]）。
3. 若【资料片段】不足以回答问题，必须原样回答："根据已有资料无法回答该问题。"，不得补充任何推测。
4. 金额、日期、编号、名称、税号等关键信息必须逐字照抄，不得改写、换算、补全或四舍五入。
5. 若多个片段之间存在冲突，需明确指出冲突，并分别标注来源编号。
6. 回答要简洁，直接给结论；不要复述本规则，不要输出与问题无关的客套话。"""


def _build_user_prompt(question: str, hits: List[Dict],
                       time_scope: Optional[Dict] = None) -> str:
    """组装用户消息：资料片段 + 问题 + （可选）时间解析提示"""
    blocks = []
    for i, h in enumerate(hits, 1):
        src = h.get("file_name") or f"文档{h.get('doc_id')}"
        page = h.get("page_no")
        loc = f"{src}（第{page}页）" if page else src
        blocks.append(f"[{i}] 来源：{loc}\n{h.get('chunk_text') or ''}")

    parts = ["【资料片段】", "\n\n".join(blocks), "", "【用户问题】", question]

    if time_scope:
        parts.append("")
        parts.append(
            f"【时间范围提示】问题中的时间表述已解析为：{time_scope['label']}"
            f"（{time_scope['start']} 至 {time_scope['end']}）。"
            f"请据此判断资料中的日期是否落在该范围内。"
        )

    parts.append("")
    parts.append("请依据上述资料作答。")
    return "\n".join(parts)


def _trim_context(hits: List[Dict], max_chars: int = MAX_CONTEXT_CHARS) -> List[Dict]:
    """
    按字符预算裁剪召回结果。

    融合排序已保证相关性递减，因此从后往前丢，直到总长度进入预算。
    至少保留 1 条 —— 否则问题会变成"无资料可答"，反而更差。
    """
    used: List[Dict] = []
    total = 0
    for h in hits:
        text = h.get("chunk_text") or ""
        if used and total + len(text) > max_chars:
            break
        used.append(h)
        total += len(text)
    return used


# ==================== 引用溯源 ====================

def _parse_citations(answer: str, hit_count: int) -> List[int]:
    """
    从回答中解析出被实际引用的编号（1-based）。

    只保留落在 [1, hit_count] 范围内的编号 ——
    模型偶尔会编出 [7] 这种不存在的编号，必须过滤掉，
    否则前端会渲染出指向空气的引用。
    """
    if not answer:
        return []
    nums = set()
    for m in re.findall(r"\[(\d{1,2})\]", answer):
        n = int(m)
        if 1 <= n <= hit_count:
            nums.add(n)
    return sorted(nums)


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
             fusion_mode: Optional[str] = None,
             vec_query: Optional[str] = None,
             bm25_query: Optional[str] = None) -> Dict:
    """
    检索入口：双路并行检索 → 分数融合 → 去重 → 阈值截断。

    参数:
      mode        — "vec" 仅向量 / "bm25" 仅关键词 / "hybrid" 双路融合
      top_k       — 最终返回条数，默认取配置 RETRIEVE_TOP_K
      doc_ids     — 限定检索的文档范围
      min_score   — 向量相似度阈值，默认取配置 RETRIEVE_MIN_SCORE
      fusion_mode — 融合策略 "rrf" / "weighted"，默认取配置 FUSION_MODE
      vec_query   — 向量路的实际查询串（默认与 query 相同）。
                    由 query_rewrite 提供，见该模块「分路投喂」的设计说明。
      bm25_query  — 关键词路的实际查询串（默认与 query 相同），通常带同义扩展。

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

    # 分路查询：未显式提供时退回同一句（保持与阶段二 /search 接口的行为兼容）
    vec_query = vec_query or query
    bm25_query = bm25_query or query

    vec_hits: List[Dict] = []
    bm25_hits: List[Dict] = []
    degraded = False
    degrade_reason = None

    # ---------- 语义路 ----------
    if mode in ("vec", "hybrid"):
        model = get_embedding_model()
        if model.is_ready or model.load():
            qv = model.encode_one(vec_query, is_query=True)
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
            bm25_hits = get_bm25_index().search(bm25_query, top_n=RETRIEVE_TOP_N_BM25)

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


# ==================== 生成段（阶段三） ====================

def _pack_references(hits: List[Dict], cited: List[int]) -> List[Dict]:
    """
    组装引用列表（1-based 编号，与回答中的 [n] 一一对应）。

    每条引用携带检索溯源信息（vec_rank / bm25_rank）与定位信息
    （char_start / char_end / page_no），供前端做高亮与"这条是哪一路召回的"展示。
    """
    refs: List[Dict] = []
    cited_set = set(cited)
    for i, h in enumerate(hits, 1):
        refs.append({
            "index": i,                                   # 对应回答中的 [i]
            "cited": i in cited_set,                      # 是否被 LLM 实际引用
            "chunk_id": h.get("chunk_id"),
            "doc_id": h.get("doc_id"),
            "file_name": h.get("file_name"),
            "page_no": h.get("page_no"),
            "chunk_index": h.get("chunk_index"),
            "snippet": h.get("snippet"),
            "chunk_text": h.get("chunk_text"),
            "chunk_text_raw": h.get("chunk_text_raw"),
            "char_start": h.get("char_start"),
            "char_end": h.get("char_end"),
            # 双路溯源：None 表示该路未召回
            "vec_rank": h.get("vec_rank"),
            "bm25_rank": h.get("bm25_rank"),
            "vec_score": h.get("vec_score"),
            "bm25_score": h.get("bm25_score"),
            "score": h.get("score"),
        })
    return refs


def _save_qa_record(db: Session, question: str, rewritten: str, answer: Optional[str],
                    hits: List[Dict], fusion_mode: Optional[str],
                    llm_model: Optional[str], latency_ms: float) -> None:
    """写入问答记录。失败不影响主流程（历史留痕是辅助能力，不该拖垮问答）。"""
    try:
        top_score = None
        for h in hits:
            if h.get("score") is not None:
                top_score = h["score"]
                break
        record = QaRecord(
            question=question,
            rewritten_q=rewritten,
            answer=answer,
            hit_chunk_ids=",".join(str(h.get("chunk_id")) for h in hits if h.get("chunk_id")),
            top_score=top_score,
            fusion_mode=fusion_mode,
            llm_model=llm_model,
            latency_ms=round(latency_ms, 1),
        )
        db.add(record)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"问答记录写入失败（不影响回答）: {e}")


def ask(db: Session, question: str, mode: str = "hybrid",
        top_k: Optional[int] = None,
        doc_ids: Optional[List[int]] = None,
        min_score: Optional[float] = None,
        fusion_mode: Optional[str] = None,
        rewrite_enable: bool = True,
        use_llm_rewrite: bool = False) -> Dict:
    """
    问答主入口：改写 → 双路检索 → 融合 → 去重 → 裁剪 → 防幻觉 Prompt → 生成 → 引用溯源。

    降级行为（三条出口，任何一条都不抛异常）：
      1. 检索为空            → 不调 LLM，直接返回 NO_HIT_TEXT
      2. LLM 未配置/调用失败 → answer 置 None，返回 LLM_DOWN_TEXT + 原文片段
      3. 向量模型不可用      → 检索层已自动降级为 BM25 单路（degraded=True）

    返回:
      {
        "answer": "...",              # LLM 回答，降级时为 None
        "answer_text": "...",         # 前端直接展示的文本（含降级提示）
        "references": [...],          # 引用列表，含 cited / vec_rank / bm25_rank
        "rewrite": {...},             # 查询改写详情，便于调试
        "retrieval": {...},           # 检索统计
        "llm": {...},                 # LLM 调用信息
        "degraded": bool,
        "degrade_reason": str | None,
        "latency_ms": float,
      }
    """
    started = time.time()

    # ---------- 1. 查询改写 ----------
    rw = rewrite_query(question, enable=rewrite_enable, use_llm=use_llm_rewrite,
                       llm_client=get_llm_client() if use_llm_rewrite else None)

    # ---------- 2. 检索 ----------
    ret = retrieve(
        db, query=rw["normalized"], mode=mode, top_k=top_k, doc_ids=doc_ids,
        min_score=min_score, fusion_mode=fusion_mode,
        vec_query=rw["vec_query"], bm25_query=rw["bm25_query"],
    )
    hits = ret["hits"]

    base = {
        "rewrite": rw,
        "retrieval": {
            "mode": ret["mode"],
            "fusion_mode": ret["fusion_mode"],
            "vec_count": ret["vec_count"],
            "bm25_count": ret["bm25_count"],
            "hit_count": len(hits),
            "min_score": ret["min_score"],
            "degraded": ret["degraded"],
            "degrade_reason": ret["degrade_reason"],
        },
    }

    # ---------- 出口 1：检索为空，不浪费一次 LLM 调用 ----------
    if not hits:
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, rw["bm25_query"], None, [], ret["fusion_mode"], None, latency_ms)
        return {
            **base,
            "answer": None,
            "answer_text": NO_HIT_TEXT,
            "references": [],
            "llm": {"called": False, "ok": False, "error": "no_hits"},
            "degraded": True,
            "degrade_reason": "no_hits",
            "latency_ms": round(latency_ms, 1),
        }

    # ---------- 3. 上下文裁剪 + 组装 Prompt ----------
    used = _trim_context(hits)
    references = _pack_references(used, [])
    user_prompt = _build_user_prompt(question, used, rw.get("time_scope"))

    # ---------- 出口 2：LLM 未配置 / 调用失败 → 降级为仅检索 ----------
    client = get_llm_client()
    if not client.is_configured:
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, rw["bm25_query"], None, used, ret["fusion_mode"], None, latency_ms)
        return {
            **base,
            "answer": None,
            "answer_text": LLM_DOWN_TEXT,
            "references": references,
            "llm": {"called": False, "ok": False, "error": "llm_not_configured",
                    "model": client.model},
            "degraded": True,
            "degrade_reason": "llm_not_configured",
            "latency_ms": round(latency_ms, 1),
        }

    resp = client.chat(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=LLM_MAX_TOKENS,
    )

    if not resp["ok"]:
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, rw["bm25_query"], None, used,
                        ret["fusion_mode"], client.model, latency_ms)
        return {
            **base,
            "answer": None,
            "answer_text": LLM_DOWN_TEXT,
            "references": references,
            "llm": {"called": True, "ok": False, "error": resp["error"],
                    "model": resp["model"], "latency_ms": resp["latency_ms"]},
            "degraded": True,
            "degrade_reason": "llm_call_failed",
            "latency_ms": round(latency_ms, 1),
        }

    # ---------- 4. 引用溯源 ----------
    answer = (resp["content"] or "").strip()
    cited = _parse_citations(answer, len(used))
    references = _pack_references(used, cited)

    latency_ms = (time.time() - started) * 1000
    _save_qa_record(db, question, rw["bm25_query"], answer, used,
                    ret["fusion_mode"], resp["model"], latency_ms)

    return {
        **base,
        "answer": answer,
        "answer_text": answer,
        "references": references,
        "cited_indexes": cited,
        "llm": {
            "called": True, "ok": True, "error": None,
            "model": resp["model"], "usage": resp["usage"],
            "latency_ms": resp["latency_ms"],
        },
        "degraded": False,
        "degrade_reason": None,
        "latency_ms": round(latency_ms, 1),
    }


# ==================== 自测 ====================

if __name__ == "__main__":
    # 查询改写自测（不依赖数据库）
    print("=" * 78)
    print("查询改写自测")
    print("=" * 78)
    fixed_now = datetime(2026, 9, 18, 9, 0, 0)
    for q in [
        "上个月打车花了多少钱",
        "2025年12月的发票金额是多少",
        "去年12月19日的出行发票",
        "最近7天的订单",
        "这个月的费用合计",
        "INV-20240115-001 这张发票的税额",
        "税号是多少",
        "票号 25447000001712590935 的出发地和到达地",
    ]:
        r = rewrite_query(q, now=fixed_now)
        scope = r["time_scope"]["label"] if r["time_scope"] else "-"
        print(f"\n原句 : {q}")
        print(f"  时间 : {scope}")
        print(f"  扩展 : {r['expansions']}")
        print(f"  vec  : {r['vec_query']}")
        print(f"  bm25 : {r['bm25_query']}")
