"""
分数融合模块

背景：
  双路检索会各返回一批候选（向量路按余弦相似度排序，关键词路按 BM25 分值排序），
  两路的分数量纲完全不同，需要融合成统一排序：

    余弦相似度：有界，落在 [0, 1]
    BM25 分值 ：无上界，取决于语料规模与 IDF 分布

为什么默认用 RRF 而不是加权求和：
  直接加权需要每次查询都做 min-max 归一化，且对异常值敏感（某一篇分特别高会压平其余）。
  RRF（Reciprocal Rank Fusion）**只看排名不看分值**，对量纲差异完全免疫，
  是 Elasticsearch / LangChain 等主流实现的标准做法。

    RRF score = Σ 1 / (k + rank)        k 默认 60

加权归一化模式同时保留，便于用评估脚本做数据驱动的对比调优。
"""
import logging
from typing import List, Dict

from app.config import RRF_K, FUSION_WEIGHT_VEC, FUSION_WEIGHT_BM25

logger = logging.getLogger(__name__)

# 从原始命中项中剔除的临时字段（保留为独立字段，避免语义混淆）
_RAW_SCORE_FIELDS = ("score", "vec_rank", "bm25_rank", "vec_score", "bm25_score")


def _base_meta(hit: Dict) -> Dict:
    """提取命中项的元信息（去掉检索过程产生的临时分数字段）"""
    return {k: v for k, v in hit.items() if k not in _RAW_SCORE_FIELDS}


def _merge_side(merged: Dict, scores: Dict, hits: List[Dict], side: str, k: int,
                weight: float = 1.0) -> None:
    """把一路检索结果并入融合结构（side ∈ {"vec", "bm25"}）"""
    for rank, hit in enumerate(hits, start=1):
        cid = hit.get("chunk_id")
        if cid is None:
            continue
        meta = merged.setdefault(cid, _base_meta(hit))
        meta[f"{side}_rank"] = rank
        meta[f"{side}_score"] = hit.get("score")
        scores[cid] = scores.get(cid, 0.0) + weight / (k + rank)


def rrf_fuse(vec_hits: List[Dict], bm25_hits: List[Dict], k: int = RRF_K,
             vec_weight: float = FUSION_WEIGHT_VEC,
             bm25_weight: float = FUSION_WEIGHT_BM25) -> List[Dict]:
    """
    RRF 融合（默认策略）。

    对每路结果，命中项的得分为 weight/(k+rank) 累加。只依赖排名，不依赖分值大小，
    因此天然规避了两路分数量纲不一致的问题。

    参数:
      k           — 平滑常数，越小则高排名的优势越明显
      vec_weight  — 向量路权重（默认 1.0）
      bm25_weight — 关键词路权重（默认 1.0），单据编号类查询为主时可调高

    ⚠️ RRF 的固有特性：**两路都命中**的条目会胜过**单路高排名**的条目。
       例如"两路均为第3名"会排在"仅向量第2名"之前。这是设计使然
       （两路一致认同是更强的信号），但若业务上更看重单路高精度，
       可调小 k 或调整两路权重。

    返回: 按融合分降序的列表，每项含
          score(RRF分) / vec_rank / bm25_rank / vec_score / bm25_score
          未命中该路时对应 rank 为 None —— 便于观察"这条是谁召回的"
    """
    merged: Dict[int, Dict] = {}
    scores: Dict[int, float] = {}

    _merge_side(merged, scores, vec_hits, "vec", k, vec_weight)
    _merge_side(merged, scores, bm25_hits, "bm25", k, bm25_weight)

    results: List[Dict] = []
    for cid, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        item = merged[cid]
        item["score"] = round(s, 6)
        item.setdefault("vec_rank", None)
        item.setdefault("bm25_rank", None)
        item.setdefault("vec_score", None)
        item.setdefault("bm25_score", None)
        results.append(item)

    logger.info(f"RRF 融合：向量路 {len(vec_hits)} 条 + 关键词路 {len(bm25_hits)} 条 "
                f"→ 去重后 {len(results)} 条")
    return results


def weighted_fuse(vec_hits: List[Dict], bm25_hits: List[Dict],
                  alpha: float = FUSION_WEIGHT_VEC,
                  beta: float = FUSION_WEIGHT_BM25) -> List[Dict]:
    """
    加权归一化融合（可选策略，用于对比调优）。

    两路各自在**本次召回结果内**做 min-max 归一化到 [0,1]，再加权求和：
        final = alpha · vec_norm + beta · bm25_norm

    注意：归一化依赖单次查询的召回分布，稳定性弱于 RRF；
    当某一路只有 1 条结果时该路归一化分恒为 1.0，需留意。
    """
    def _minmax(hits: List[Dict]) -> Dict[int, float]:
        if not hits:
            return {}
        vals = [h.get("score") or 0.0 for h in hits]
        lo, hi = min(vals), max(vals)
        rng = hi - lo
        if rng <= 1e-9:
            return {h["chunk_id"]: 1.0 for h in hits}
        # ⚠️ 运算符优先级坑（方案 B 修复）：`h.get("score") or 0.0 - lo` 会被解析为
        #    `score or (0.0 - lo)` —— score 非零时 lo 根本没被减掉，归一化结果全错。
        #    正确形式必须先取 score 的 or 默认值、再整体减 lo。
        return {h["chunk_id"]: ((h.get("score") or 0.0) - lo) / rng for h in hits}

    merged: Dict[int, Dict] = {}
    scores: Dict[int, float] = {}

    vec_norm = _minmax(vec_hits)
    bm25_norm = _minmax(bm25_hits)

    for rank, hit in enumerate(vec_hits, start=1):
        cid = hit.get("chunk_id")
        if cid is None:
            continue
        meta = merged.setdefault(cid, _base_meta(hit))
        meta["vec_rank"] = rank
        meta["vec_score"] = hit.get("score")
        scores[cid] = scores.get(cid, 0.0) + alpha * vec_norm.get(cid, 0.0)

    for rank, hit in enumerate(bm25_hits, start=1):
        cid = hit.get("chunk_id")
        if cid is None:
            continue
        meta = merged.setdefault(cid, _base_meta(hit))
        meta["bm25_rank"] = rank
        meta["bm25_score"] = hit.get("score")
        scores[cid] = scores.get(cid, 0.0) + beta * bm25_norm.get(cid, 0.0)

    results: List[Dict] = []
    for cid, s in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        item = merged[cid]
        item["score"] = round(s, 6)
        item.setdefault("vec_rank", None)
        item.setdefault("bm25_rank", None)
        item.setdefault("vec_score", None)
        item.setdefault("bm25_score", None)
        results.append(item)

    logger.info(f"加权融合(α={alpha}, β={beta})：向量路 {len(vec_hits)} 条 + "
                f"关键词路 {len(bm25_hits)} 条 → 去重后 {len(results)} 条")
    return results


def fuse(vec_hits: List[Dict], bm25_hits: List[Dict], mode: str = "rrf", **kwargs) -> List[Dict]:
    """
    融合入口。mode ∈ {"rrf", "weighted"}，非法值回退到 rrf。

    rrf 模式可用 kwargs 传 k / vec_weight / bm25_weight
    weighted 模式可用 kwargs 传 alpha / beta
    """
    if mode == "weighted":
        return weighted_fuse(vec_hits, bm25_hits, **kwargs)
    if mode != "rrf":
        logger.warning(f"未知融合模式 {mode!r}，回退到 rrf")
    return rrf_fuse(vec_hits, bm25_hits, **kwargs)


if __name__ == "__main__":
    # 自测：验证两路排名如何影响融合结果
    # 运行: .venv/Scripts/python.exe -m app.services.fusion
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def hit(cid, doc_id, name, score):
        return {"chunk_id": cid, "doc_id": doc_id, "file_name": name,
                "chunk_text": f"分块{cid}内容", "page_no": None, "score": score}

    # 构造一个能体现融合价值的场景：
    #   A 两路都靠前（应排第一）
    #   B 只有向量路命中（语义相关）
    #   C 只有关键词路命中（精确匹配）
    vec_hits = [
        hit(1, 1, "A.pdf", 0.91),
        hit(2, 2, "B.pdf", 0.88),
        hit(3, 3, "D.pdf", 0.40),
    ]
    bm25_hits = [
        hit(1, 1, "A.pdf", 12.7),
        hit(4, 4, "C.pdf", 9.3),
        hit(3, 3, "D.pdf", 2.1),
    ]

    print("=" * 78)
    print("输入")
    print("=" * 78)
    print("  向量路:", [(h["file_name"], h["score"]) for h in vec_hits])
    print("  关键词路:", [(h["file_name"], h["score"]) for h in bm25_hits])

    print()
    print("=" * 78)
    print("RRF 融合（默认）—— 只看排名不看分值")
    print("=" * 78)
    print(f"  {'排名':<4} {'文件':<10} {'RRF分':>9} {'向量名次':>9} {'关键词名次':>11}  召回来源")
    for i, h in enumerate(rrf_fuse(vec_hits, bm25_hits), start=1):
        vr = h["vec_rank"] or "-"
        br = h["bm25_rank"] or "-"
        src = "两路" if h["vec_rank"] and h["bm25_rank"] else ("仅向量" if h["vec_rank"] else "仅关键词")
        print(f"  {i:<4} {h['file_name']:<10} {h['score']:>9.5f} {str(vr):>9} {str(br):>11}  {src}")

    print()
    print("=" * 78)
    print("加权归一化融合（可选）α=0.5 β=0.5")
    print("=" * 78)
    print(f"  {'排名':<4} {'文件':<10} {'融合分':>9} {'向量名次':>9} {'关键词名次':>11}  召回来源")
    for i, h in enumerate(weighted_fuse(vec_hits, bm25_hits), start=1):
        vr = h["vec_rank"] or "-"
        br = h["bm25_rank"] or "-"
        src = "两路" if h["vec_rank"] and h["bm25_rank"] else ("仅向量" if h["vec_rank"] else "仅关键词")
        print(f"  {i:<4} {h['file_name']:<10} {h['score']:>9.5f} {str(vr):>9} {str(br):>11}  {src}")

    print()
    print("说明：两路都命中的 A 排第一；仅向量命中的 B 与仅关键词命中的 C 各自保留，")
    print("      这正是双路互补的价值 —— 单路都会漏掉对方能召回的内容。")
