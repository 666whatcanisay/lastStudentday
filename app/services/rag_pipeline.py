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
    RETRIEVE_MIN_SCORE, RETRIEVE_SCORE_MARGIN, RETRIEVE_NO_HIT_SCORE,
    RETRIEVE_BM25_COVERAGE_MIN,
    FUSION_MODE, MAX_CONTEXT_CHARS, LLM_MAX_TOKENS,
)
from app.models import QaRecord
from app.services.bm25_index import get_bm25_index
from app.services.embedding import get_embedding_model
from app.services.fusion import fuse
from app.services.llm_client import get_llm_client
from app.services import revisions as rev_svc
from app.services.query_rewrite import resolve_coreference
from app.services.query_rewrite import rewrite as rewrite_query
from app.services.rag_indexer import ensure_bm25_ready
from app.services.vector_store import (
    search_vector, get_eligible_doc_ids, filter_stale_hits,
)
from app.services.backup_gate import GATE

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

# 回答生成期间资料失效时的提示（任务书 3.2：
# 云端请求一旦发出无法撤回，但可以不把它当有效回答展示或记录）
STALE_ANSWER_TEXT = (
    "（生成回答期间，所依据的资料已被修改或删除，本次结果已作废。"
    "请重新提问后再查看。）"
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
                       time_scope: Optional[Dict] = None,
                       coref: Optional[Dict] = None) -> str:
    """组装用户消息：资料片段 + 问题 + （可选）指代消解/时间解析提示"""
    blocks = []
    for i, h in enumerate(hits, 1):
        src = h.get("file_name") or f"文档{h.get('doc_id')}"
        page = h.get("page_no")
        loc = f"{src}（第{page}页）" if page else src
        blocks.append(f"[{i}] 来源：{loc}\n{h.get('chunk_text') or ''}")

    parts = ["【资料片段】", "\n\n".join(blocks), "", "【用户问题】", question]

    # 多轮指代提示：告诉模型「它 / 这张发票」指的是哪份文档。
    # 用原问题提问（保留用户真实措辞），但把指代关系显式说清楚，
    # 比直接把问题替换掉更稳 —— 模型既知道用户原话，也知道指向。
    if coref and coref.get("changed"):
        focus = coref.get("focus") or {}
        pronoun = coref.get("pronoun")
        if pronoun:
            desc = f"本轮问题中的「{pronoun}」指代文档：{focus.get('file_name') or ''}"
        else:
            desc = f"本轮问题是承接上一轮的省略式追问，所指文档为：{focus.get('file_name') or ''}"
        if focus.get("anchor"):
            desc += f"（唯一标识：{focus['anchor']}）"
        parts.append("")
        parts.append(f"【指代提示】{desc}。请按此理解作答。")

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


def _empty_retrieval(mode: str, fusion_mode: str, min_score: float,
                     score_margin: float, bm25_coverage_min: float,
                     no_hit: bool = True) -> Dict:
    """统一的空结果结构 —— 保证上层拿到的字段永远齐全，不用到处判空"""
    return {
        "hits": [],
        "mode": mode,
        "fusion_mode": fusion_mode if mode == "hybrid" else None,
        "vec_count": 0,
        "bm25_count": 0,
        "min_score": min_score,
        "score_margin": score_margin,
        "vec_top1": 0.0,
        "bm25_coverage": 0.0,
        "bm25_coverage_min": bm25_coverage_min,
        "doc_scope_count": 0,
        "dropped_stale": 0,
        "no_hit": no_hit,
        "degraded": False,
        "degrade_reason": None,
    }


def retrieve(db: Session, query: str, mode: str = "hybrid",
             top_k: Optional[int] = None,
             doc_ids: Optional[List[int]] = None,
             min_score: Optional[float] = None,
             fusion_mode: Optional[str] = None,
             vec_query: Optional[str] = None,
             bm25_query: Optional[str] = None,
             score_margin: Optional[float] = None,
             no_hit_score: Optional[float] = None,
             bm25_coverage_min: Optional[float] = None) -> Dict:
    """
    检索入口：双路并行检索 → 分数融合 → 去重 → 阈值截断。

    参数:
      mode         — "vec" 仅向量 / "bm25" 仅关键词 / "hybrid" 双路融合
      top_k        — 最终返回条数，默认取配置 RETRIEVE_TOP_K
      doc_ids      — 限定检索的文档范围（详见 get_eligible_doc_ids）：
                    不传 / None = 全部**可用**文档；
                    空列表 [] = 明确不要任何文档，**绝不扩大为全部**。
                    可用 = 未删除 + 有当前有效版本 + 索引指向该版本。
                    范围在向量路与关键词路**召回前**生效，并在最终结果再次校验。
      min_score    — 向量相似度**噪声下限**，默认取配置 RETRIEVE_MIN_SCORE
      fusion_mode  — 融合策略 "rrf" / "weighted"，默认取配置 FUSION_MODE
      score_margin — 向量路**相对带宽**，默认取配置 RETRIEVE_SCORE_MARGIN。
                     实际截断线 = max(min_score, top1 - score_margin)。
                     为什么不能只用绝对阈值：bge-zh 相似度是长度相关的，
                     短查询 top1 只有 0.34 而长查询有 0.64，单一死线要么
                     掐断短查询、要么放过长查询的噪声。详见 config.py 注释。
      vec_query   — 向量路的实际查询串（默认与 query 相同）。
                    由 query_rewrite 提供，见该模块「分路投喂」的设计说明。
      bm25_query  — 关键词路的实际查询串（默认与 query 相同），通常带同义扩展。
      no_hit_score       — 无结果判定时向量路的置信度下限，默认取配置。
      bm25_coverage_min  — 无结果判定时关键词路的**覆盖率**下限，默认取配置。
                           覆盖率 = bm25_top1 / Σ IDF(查询的每个词)。
                           用覆盖率而非"零命中"的原因见 config.py 长注释。

    返回:
      {
        "hits": [...],           # 去重截断后的结果，含 vec_rank / bm25_rank
        "mode": "hybrid",
        "fusion_mode": "rrf",
        "vec_count": 12,         # 向量路原始召回数
        "bm25_count": 7,         # 关键词路原始召回数
        "vec_top1": 0.3958,      # 向量路最高分（诊断用）
        "bm25_coverage": 0.026,  # 关键词路覆盖率（诊断用）
        "no_hit": False,         # 是否判定为"检索不到相关内容"
        "degraded": False,       # 是否因模型不可用而降级
        "degrade_reason": None,
      }
    """
    top_k = top_k or RETRIEVE_TOP_K
    min_score = RETRIEVE_MIN_SCORE if min_score is None else min_score
    score_margin = RETRIEVE_SCORE_MARGIN if score_margin is None else score_margin
    no_hit_score = RETRIEVE_NO_HIT_SCORE if no_hit_score is None else no_hit_score
    bm25_coverage_min = (RETRIEVE_BM25_COVERAGE_MIN if bm25_coverage_min is None
                         else bm25_coverage_min)
    fusion_mode = fusion_mode or FUSION_MODE

    # ---------- 检索范围 ----------
    # 只召回"当前版本索引已就绪且未被删除"的文档；索引未就绪的不拿旧索引兜底。
    # 空列表是用户明确指定的空范围，直接返回空，不静默扩大成全库。
    scope = get_eligible_doc_ids(db, doc_ids)
    if doc_ids is not None and len(doc_ids) == 0:
        logger.info("检索范围为空列表（明确无文档）：返回空结果")
        return _empty_retrieval(mode, fusion_mode, min_score, score_margin,
                                bm25_coverage_min, no_hit=True)

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
                vec_hits = search_vector(db, qv, top_n=RETRIEVE_TOP_N_VEC, doc_ids=scope)

                # 两级过滤：绝对噪声下限 + 相对 top1 带宽
                #   截断线 = max(min_score, top1 - score_margin)
                # 相对项让过滤强度自适应：长查询 top1 高 → 带宽收紧、切掉噪声；
                # 短口语查询 top1 低 → 带宽放宽、保住候选。
                if vec_hits:
                    top1 = vec_hits[0]["score"]
                    cutoff = max(min_score, top1 - score_margin)
                    before = len(vec_hits)
                    vec_hits = [h for h in vec_hits if h["score"] >= cutoff]
                    if before != len(vec_hits):
                        logger.info(
                            f"向量路截断(top1={top1:.4f}, 带宽={score_margin}, "
                            f"实际线={cutoff:.4f}): {before} → {len(vec_hits)} 条"
                        )
        else:
            degraded = True
            degrade_reason = "vector_model_unavailable"
            logger.warning("向量模型不可用，本次检索降级为 BM25 单路")

    # ---------- 关键词路 ----------
    # bm25_coverage = top1 分 / 查询 IDF 总量，衡量"最好的文档满足了这条查询
    # 多少比例的信息需求"。它是**比值**，不像原始分那样随语料规模缩放，
    # 所以能做阈值。详见 config.RETRIEVE_BM25_COVERAGE_MIN 的长注释。
    bm25_coverage = 0.0
    if mode in ("bm25", "hybrid"):
        if ensure_bm25_ready(db):
            idx = get_bm25_index()
            bm25_hits = idx.search(bm25_query, top_n=RETRIEVE_TOP_N_BM25, doc_ids=scope)
            if bm25_hits:
                mass = idx.query_idf_mass(bm25_query)
                if mass > 0:
                    bm25_coverage = bm25_hits[0]["score"] / mass

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

    # ---------- 无结果判定 ----------
    # 判定权交给关键词路（它有真实判别力），向量路只做高置信度兜底。
    # 原因见 config.py 的长注释：本语料上向量分无法区分相关/无关。
    #
    # ⚠️ 只在 hybrid（生产链路）上启用这个门控。
    #    单路模式（vec / bm25）是诊断视图，本就用于观察"这一路召回了什么"，
    #    不应替用户下"没有结果"的结论 —— 尤其 vec 单路缺少 BM25 的判别力，
    #    套用门控会把「从哪到哪」(0.3787) 这类相关的短查询误判成无结果。
    vec_top1 = vec_hits[0]["score"] if vec_hits else 0.0
    if mode == "hybrid":
        # 关键词路这一侧从「零命中」改成「覆盖率低于下限」。
        # 零命中是个二元条件，查询含任一常见词就必然有命中，门控直接被关掉；
        # 覆盖率能表达"命中了，但只满足了很小一部分信息需求"这个中间状态。
        # 覆盖率天然涵盖零命中（无命中 → 0.0 < 阈值）。
        no_hit = (bm25_coverage < bm25_coverage_min) and (vec_top1 < no_hit_score)
    else:
        no_hit = len(fused) == 0

    # ---------- 去重、版本校验与截断 ----------
    # 顺序有讲究：先去重 → 再剔除过期/已删除 → 最后才截断。
    # 若先截断再校验，被剔除的空位不会补位，Top-K 名额白白浪费。
    deduped = _dedup(fused)
    validated = filter_stale_hits(db, deduped)
    dropped = len(deduped) - len(validated)
    final = [] if no_hit else validated[:top_k]

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
        "score_margin": score_margin,
        "vec_top1": round(vec_top1, 4),
        "bm25_coverage": round(bm25_coverage, 4),
        "bm25_coverage_min": bm25_coverage_min,
        "doc_scope_count": len(scope),
        "dropped_stale": dropped,
        "no_hit": no_hit,
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
            # 方案 B：来源归属 —— 前端据此区分"正文引用"与"结构化字段"，
            # 并保证引用不会被误认为当前版本之外的旧内容
            "revision": h.get("revision"),
            "source_kind": h.get("source_kind") or "text",
        })
    return refs


def _save_qa_record(db: Session, question: str, rewritten: str, answer: Optional[str],
                    hits: List[Dict], fusion_mode: Optional[str],
                    llm_model: Optional[str], latency_ms: float) -> None:
    """写入问答记录。失败不影响主流程（历史留痕是辅助能力，不该拖垮问答）。

    走写入门禁：备份进行中的记录写不进去，宁可丢一条历史也不破坏备份一致性。
    """
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
        with GATE.write_section("qa_record"):
            db.add(record)
            db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"问答记录写入失败（不影响回答）: {e}")


def _refetch_head_text(db: Session, doc_id: int, limit: int = 800) -> Optional[str]:
    """按 doc_id 从当前有效版本重取资料片段（替代客户端带来的旧片段）"""
    try:
        snap = rev_svc.get_effective_snapshot(db, doc_id)
        if snap and snap.effective_text:
            return snap.effective_text[:limit]
    except Exception as e:
        logger.warning(f"重取文档 {doc_id} 有效文本失败: {e}")
    return None


def _reseat_history(db: Session, history: Optional[List[Dict]],
                    scope_ids: List[int]) -> List[Dict]:
    """
    重定位多轮历史里的 Top-1 资料（任务书 3.2）。

    客户端带来的 top_chunk_text **不可信**：它可能是旧版本的片段，
    也可能对应的文档早已被删除。后端只保留 doc_id 这一事实，
    文本按"当前仍有效的文档范围"重新取回来；不在范围内的条目清空文本（
    取不到锚点，指代自然回到常规检索），并把 doc_id 记入 stale_history_docs，
    让前端把旧会话引用标成"旧版本/已删除"。
    """
    if not history:
        return []

    scope = set(scope_ids or [])
    reseated: List[Dict] = []
    for entry in history:
        e = dict(entry)
        doc_id = e.get("top_doc_id")
        if doc_id is not None:
            e["top_chunk_text"] = (_refetch_head_text(db, doc_id)
                                   if doc_id in scope else None)
        reseated.append(e)
    return reseated


def ask(db: Session, question: str, mode: str = "hybrid",
        top_k: Optional[int] = None,
        doc_ids: Optional[List[int]] = None,
        min_score: Optional[float] = None,
        fusion_mode: Optional[str] = None,
        rewrite_enable: bool = True,
        use_llm_rewrite: bool = False,
        history: Optional[List[Dict]] = None) -> Dict:
    """
    问答主入口：指代消解 → 查询改写 → 双路检索 → 融合 → 去重 → 裁剪
                → 防幻觉 Prompt → 生成 → 引用溯源。

    多轮：history 由前端随请求带来（后端无状态），格式
          [{"question":..,"answer":..,"top_doc_id":..,"top_file_name":..,
            "top_chunk_text":..}, ...]

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

    # ---------- 0. 会话资料重定位 + 多轮指代消解 ----------
    # 先算一次可用范围：既用于历史片段重取，也用于随后的检索过滤。
    # 客户端带来的旧 top_chunk_text 一律不直接采信（任务书 3.2）。
    scope = get_eligible_doc_ids(db, doc_ids)
    history = _reseat_history(db, history, scope)
    stale_history_docs = sorted({
        e.get("top_doc_id") for e in history
        if e.get("top_doc_id") is not None and not e.get("top_chunk_text")
    })

    coref = resolve_coreference(
        question, history,
        use_llm=use_llm_rewrite,
        llm_client=get_llm_client() if use_llm_rewrite else None,
    )
    search_question = coref["resolved"] if coref["changed"] else question

    # ---------- 1. 查询改写 ----------
    rw = rewrite_query(search_question, enable=rewrite_enable, use_llm=use_llm_rewrite,
                       llm_client=get_llm_client() if use_llm_rewrite else None)

    # ---------- 2. 检索 ----------
    ret = retrieve(
        db, query=rw["normalized"], mode=mode, top_k=top_k, doc_ids=doc_ids,
        min_score=min_score, fusion_mode=fusion_mode,
        vec_query=rw["vec_query"], bm25_query=rw["bm25_query"],
    )
    hits = ret["hits"]

    base = {
        "coreference": coref,
        "rewrite": rw,
        "stale_history_docs": stale_history_docs,
        "retrieval": {
            "mode": ret["mode"],
            "fusion_mode": ret["fusion_mode"],
            "vec_count": ret["vec_count"],
            "bm25_count": ret["bm25_count"],
            "hit_count": len(hits),
            "min_score": ret["min_score"],
            "score_margin": ret["score_margin"],
            "vec_top1": ret["vec_top1"],
            "bm25_coverage": ret["bm25_coverage"],
            "bm25_coverage_min": ret["bm25_coverage_min"],
            "doc_scope_count": ret["doc_scope_count"],
            "dropped_stale": ret["dropped_stale"],
            "no_hit": ret["no_hit"],
            "degraded": ret["degraded"],
            "degrade_reason": ret["degrade_reason"],
        },
    }

    # ---------- 出口 1：检索为空，不浪费一次 LLM 调用 ----------
    if not hits:
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, search_question, None, [], ret["fusion_mode"], None, latency_ms)
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

    # ---------- 3. 上下文裁剪 ----------
    used = _trim_context(hits)

    # ---------- 3.1 云端请求前的最后核查（任务书 3.2） ----------
    # 从检索到现在已经过了分词、向量化、融合、裁剪的时间，
    # 文档可能恰好在此期间被修改或删除。此刻核对，避免把原文送去云端。
    used = filter_stale_hits(db, used)
    if not used:
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, search_question, None, [], ret["fusion_mode"],
                        None, latency_ms)
        return {
            **base,
            "answer": None,
            "answer_text": STALE_ANSWER_TEXT,
            "references": [],
            "llm": {"called": False, "ok": False, "error": "references_invalidated"},
            "degraded": True,
            "degrade_reason": "references_invalidated",
            "latency_ms": round(latency_ms, 1),
        }

    references = _pack_references(used, [])
    user_prompt = _build_user_prompt(question, used, rw.get("time_scope"), coref)

    # ---------- 出口 2：LLM 未配置 / 调用失败 → 降级为仅检索 ----------
    client = get_llm_client()
    if not client.is_configured:
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, search_question, None, used, ret["fusion_mode"], None, latency_ms)
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
        _save_qa_record(db, question, search_question, None, used,
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

    # 4.1 返回前再核查一次：云端请求已经发出无法撤回，但可以选择不把它当成有效回答。
    #     只要依据的资料有一条失效，就整篇作废 —— 半新半旧的回答比没有回答更危险。
    revalidated = filter_stale_hits(db, used)
    if len(revalidated) != len(used):
        latency_ms = (time.time() - started) * 1000
        _save_qa_record(db, question, search_question, None, [], ret["fusion_mode"],
                        resp["model"], latency_ms)
        logger.warning("回答生成期间依据资料已变更，本次云端回答已作废")
        return {
            **base,
            "answer": None,
            "answer_text": STALE_ANSWER_TEXT,
            "references": _pack_references(revalidated, []),
            "cited_indexes": [],
            "llm": {"called": True, "ok": False, "error": "references_invalidated",
                    "model": resp["model"]},
            "degraded": True,
            "degrade_reason": "references_invalidated",
            "latency_ms": round(latency_ms, 1),
        }

    references = _pack_references(used, cited)

    latency_ms = (time.time() - started) * 1000
    _save_qa_record(db, question, search_question, answer, used,
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
