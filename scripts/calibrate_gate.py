#!/usr/bin/env python
"""
无结果门控的阈值标定脚本（RETRIEVE_BM25_COVERAGE_MIN / RETRIEVE_NO_HIT_SCORE）

为什么需要单独一个脚本
----------------------
1. eval_rag.py 的 --sweep **只扫 no_hit_score，不扫覆盖率**；而覆盖率下限
   RETRIEVE_BM25_COVERAGE_MIN 是**语料相关**的经验值（见 config.py 的长注释），
   语料一扩充就必须重标。
2. 两个阈值在门控里是**且**关系：

       no_hit = (bm25_coverage < cov_min) and (vec_top1 < no_hit_score)

   所以必须**二维一起看**。单扫一个会得出错误结论 —— 例如"把 cov_min 调高
   能拦住更多负样本"，但同时会误杀覆盖率低的正样本；而如果那条正样本的
   vec_top1 很高，因为"且"的关系它其实**根本不会被误杀**。一维扫描看不到这点。

做法
----
1. 用 `bm25_coverage_min=0.0` 跑一遍**真实检索** —— `cov >= 0` 恒真，门控必然
   不触发，于是拿到的 hits 就是**未被门控裁剪**的完整融合排序，同时带回两个
   诊断量 (bm25_coverage, vec_top1)。注意走的仍是生产链路：rewrite → 分路投喂。
2. 用采集到的数据**离线重放**门控公式，扫描二维网格，对每个组合报告：
     · 负样本正确率   —— 判为"无结果"的负样本比例，越高越好
     · 正样本误杀数   —— 被误判为"无结果"的正样本条数，越低越好（主要代价）
     · 有效 Recall@1/@5 —— 被误杀的正样本计为 miss，这才是用户真实体验

⚠️ 本脚本**不改任何配置**，只产出建议值。定稿要手改 app/config.py 的默认值，
   然后用 scripts/eval_rag.py 复核。

用法：
  ./.venv/Scripts/python.exe scripts/calibrate_gate.py
  ./.venv/Scripts/python.exe scripts/calibrate_gate.py --mode hybrid+rw
"""
import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 GBK，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import (  # noqa: E402
    RETRIEVE_BM25_COVERAGE_MIN,
    RETRIEVE_NO_HIT_SCORE,
    RETRIEVE_TOP_K,
)
from app.database import SessionLocal  # noqa: E402
from app.services.query_rewrite import rewrite as rewrite_query  # noqa: E402
from app.services.rag_pipeline import retrieve  # noqa: E402

from eval_rag import (  # noqa: E402
    K_LIST,
    dwidth,
    first_relevant_rank,
    load_eval_set,
    pad,
    rpad,
)

# 覆盖率候选值。当前值 0.045 也在其中，便于对照。
COV_CANDIDATES = (0.030, 0.045, 0.060, 0.080, 0.090, 0.100,
                  0.110, 0.120, 0.140, 0.160, 0.200)
# 向量置信度候选值。当前值 0.45 也在其中。
NHS_CANDIDATES = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)


# ==================== 采集 ====================

def collect(db, queries: List[Dict], mode: str) -> List[Dict]:
    """跑一遍真实检索，采集未被门控裁剪的排序与两个诊断量。

    关键手法：bm25_coverage_min=0.0 → `coverage < 0.0` 恒为 False
    → no_hit 恒为 False → hits 就是完整的融合排序。
    这样只需跑一遍检索，就能离线重放**任意**阈值组合。
    """
    base_mode = mode.replace("+rw", "")
    if base_mode != "hybrid":
        raise ValueError(
            f"门控只在 hybrid 生效（见 rag_pipeline.retrieve 注释），"
            f"当前 mode={mode!r} 标定无意义"
        )

    rows: List[Dict] = []
    for q in queries:
        kwargs = {"mode": base_mode, "top_k": RETRIEVE_TOP_K}
        if mode.endswith("+rw"):
            rw = rewrite_query(q["query"])
            kwargs["vec_query"] = rw.get("vec_query") or q["query"]
            kwargs["bm25_query"] = rw.get("bm25_query") or q["query"]

        r = retrieve(db, query=q["query"], bm25_coverage_min=0.0, **kwargs)
        rows.append({
            "id": q["id"],
            "query": q["query"],
            "category": q.get("category", ""),
            "expected": q["expected_doc_ids"],
            "is_neg": not q["expected_doc_ids"],
            "doc_ids": [h.get("doc_id") for h in r["hits"]],
            "coverage": r.get("bm25_coverage", 0.0),
            "vec_top1": r.get("vec_top1", 0.0),
            "degraded": r["degraded"],
        })
    return rows


# ==================== 离线重放 ====================

def replay(rows: List[Dict], cov_min: float, nhs: float) -> Dict:
    """按生产公式重放门控，算三个指标。"""
    n_pos = n_neg = 0
    neg_blocked = 0
    pos_killed = 0
    recall_hits = {k: 0 for k in K_LIST}
    rr_sum = 0.0

    for r in rows:
        # 与 rag_pipeline.py 第 321 行完全一致
        no_hit = (r["coverage"] < cov_min) and (r["vec_top1"] < nhs)
        if r["is_neg"]:
            n_neg += 1
            if no_hit:
                neg_blocked += 1
        else:
            n_pos += 1
            if no_hit:
                # 被误杀：用户什么都拿不到 → 所有 K 都不命中
                pos_killed += 1
                continue
            rank = first_relevant_rank(r["doc_ids"], r["expected"])
            rr_sum += (1.0 / rank) if rank else 0.0
            for k in K_LIST:
                if any(d in r["expected"] for d in r["doc_ids"][:k]):
                    recall_hits[k] += 1

    return {
        "cov_min": cov_min,
        "nhs": nhs,
        "neg_acc": (neg_blocked / n_neg) if n_neg else 0.0,
        "neg_blocked": neg_blocked,
        "n_neg": n_neg,
        "pos_killed": pos_killed,
        "n_pos": n_pos,
        "recall": {k: (recall_hits[k] / n_pos) if n_pos else 0.0 for k in K_LIST},
        "mrr": (rr_sum / n_pos) if n_pos else 0.0,
    }


# ==================== 展示 ====================

def print_distribution(rows: List[Dict]):
    """按覆盖率排序打印，让分离区间一目了然。"""
    print()
    print("=" * 100)
    print("一、实测分布（走生产链路：rewrite → 分路投喂；门控已关闭以取全量排序）")
    print("=" * 100)

    negs = sorted([r for r in rows if r["is_neg"]], key=lambda x: -x["coverage"])
    poss = sorted([r for r in rows if not r["is_neg"]], key=lambda x: x["coverage"])

    print()
    print(f"负样本（{len(negs)} 条）—— 按覆盖率降序，最该被拦的在最上面")
    print("-" * 100)
    print(f"{'#':>3} {'覆盖':>8}{'vec_top1':>10}   query")
    for r in negs:
        print(f"{r['id']:>3} {r['coverage']:>8.4f}{r['vec_top1']:>10.4f}   {r['query']}")

    print()
    print(f"正样本（{len(poss)} 条）—— 按覆盖率升序，最容易被误杀的在最上面")
    print("-" * 100)
    print(f"{'#':>3} {'覆盖':>8}{'vec_top1':>10}  {'位次':>4}   query")
    for r in poss:
        rank = first_relevant_rank(r["doc_ids"], r["expected"])
        print(f"{r['id']:>3} {r['coverage']:>8.4f}{r['vec_top1']:>10.4f}"
              f"  {rank:>4}   {r['query']}")

    # 分离区间
    if negs and poss:
        neg_max = max(r["coverage"] for r in negs)
        pos_min = min(r["coverage"] for r in poss)
        print()
        print(f"覆盖率分离区间：负样本最高 {neg_max:.4f}  |  正样本最低 {pos_min:.4f}")
        if neg_max < pos_min:
            geo = (neg_max * pos_min) ** 0.5
            print(f"  区间宽 {pos_min - neg_max:.4f}，几何中点 ≈ {geo:.4f}（仅覆盖率单维的参考值）")
        else:
            print("  ⚠️ 覆盖率单维**不可分**（区间重叠）—— 必须靠与 vec_top1 的"
                  "「且」关系配合，见二维扫描")


def print_grid(rows: List[Dict]):
    """二维扫描：行 = cov_min，列 = no_hit_score。"""
    print()
    print("=" * 100)
    print("二、二维阈值扫描（行=覆盖率下限，列=向量置信度下限）")
    print("=" * 100)
    print("单元格含义：负样本正确率 / 正样本误杀数 / 有效 Recall@5")
    print("（有效 = 被误杀的正样本计为 miss，即用户真实拿到的结果）")
    print()

    header = pad("cov_min", 10) + "".join(f"{f'{n:.2f}':>22}" for n in NHS_CANDIDATES)
    print(header)
    print("-" * dwidth(header))

    best = None
    for cov in COV_CANDIDATES:
        line = pad(f"{cov:.3f}", 10)
        for nhs in NHS_CANDIDATES:
            m = replay(rows, cov, nhs)
            cell = f"{m['neg_acc']*100:.0f}%/{m['pos_killed']}/{m['recall'][5]*100:.0f}%"
            line += f"{cell:>22}"
            # 选择判据：先保证正样本零误杀，再最大化负样本正确率，最后看 Recall@5
            score = (-m["pos_killed"], m["neg_acc"], m["recall"][5], m["mrr"])
            if best is None or score > best[0]:
                best = (score, m)
        print(line)

    print()
    print("说明：当前生产值 = "
          f"cov_min {RETRIEVE_BM25_COVERAGE_MIN} / nhs {RETRIEVE_NO_HIT_SCORE}"
          f"  →  负样本正确率 {replay(rows, RETRIEVE_BM25_COVERAGE_MIN, RETRIEVE_NO_HIT_SCORE)['neg_acc']*100:.0f}%"
          f"，误杀 {replay(rows, RETRIEVE_BM25_COVERAGE_MIN, RETRIEVE_NO_HIT_SCORE)['pos_killed']} 条")

    print()
    print("=" * 100)
    print("三、推荐值")
    print("=" * 100)
    m = best[1]
    print(f"推荐组合：cov_min = {m['cov_min']:.3f}   no_hit_score = {m['nhs']:.2f}")
    print(f"  → 负样本正确率 {m['neg_acc']*100:.1f}%"
          f"（{m['neg_blocked']}/{m['n_neg']}）"
          f"   正样本误杀 {m['pos_killed']} 条"
          f"   有效 Recall@1 {m['recall'][1]*100:.1f}%"
          f" / Recall@5 {m['recall'][5]*100:.1f}%"
          f"   MRR {m['mrr']:.4f}")
    print()
    print("⚠️ 风险不对称（见 config.py）：阈值偏高 → 合法查询被判无结果（严重）；")
    print("   阈值偏低 → 多返回几条勉强相关的片段，LLM 通常还能自行拒答（轻微）。")
    print("   因此在指标接近的候选里**优先选更低的那个**。")

    print_margins(rows, m["cov_min"], m["nhs"])

    print()
    print("⚠️ 语料再扩充后必须重跑本脚本 —— 覆盖率是语料相关量。")


def print_margins(rows: List[Dict], cov_min: float, nhs: float):
    """逐维报告「选定值离最近的样本有多远」。

    这一步是**必须的**：只看「负样本正确率涨了」就改生产配置是评估纪律里
    明令禁止的（n=1 的样本不足以改配置）。必须同时看余量 ——
    如果选定值贴着某个样本，那它随时会被一条新样本掀翻。
    """
    negs = [r for r in rows if r["is_neg"]]
    poss = [r for r in rows if not r["is_neg"]]
    if not negs or not poss:
        return

    print()
    print("=" * 100)
    print("四、余量分析（选定值离最近样本有多远）")
    print("=" * 100)

    # --- 覆盖率维度 ---
    cov_neg_max = max(r["coverage"] for r in negs)
    cov_pos_min = min(r["coverage"] for r in poss)
    print()
    print(f"覆盖率维度   cov_min = {cov_min:.3f}")
    print(f"  负样本上界 {cov_neg_max:.4f}  →  余量 {cov_min - cov_neg_max:+.4f}"
          f"（{(cov_min / cov_neg_max - 1) * 100:+.1f}%，越高越好拦）")
    print(f"  正样本下界 {cov_pos_min:.4f}  →  余量 {cov_pos_min - cov_min:+.4f}"
          f"（{(cov_pos_min / cov_min - 1) * 100:+.1f}%，越低越安全）")
    if cov_neg_max < cov_min < cov_pos_min:
        print("  ✓ 覆盖率单维干净分离，且两侧都有余量")
    else:
        print("  ⚠️ 选定值未落在分离区间内 —— 覆盖率单维不可分，判别完全依赖另一维")

    # --- 向量维度 ---
    vec_neg_max = max(r["vec_top1"] for r in negs)
    vec_pos_min = min(r["vec_top1"] for r in poss)
    print()
    print(f"向量维度     no_hit_score = {nhs:.2f}")
    print(f"  负样本上界 {vec_neg_max:.4f}  →  余量 {nhs - vec_neg_max:+.4f}")
    print(f"  正样本下界 {vec_pos_min:.4f}  →  余量 {vec_pos_min - nhs:+.4f}")
    if vec_neg_max < nhs < vec_pos_min:
        print("  ✓ 向量单维也可分")
    elif nhs >= vec_pos_min:
        # 这是「且」关系的固有代价，必须说清楚，否则将来会被误读成 bug
        victims = [r for r in poss if r["vec_top1"] < nhs]
        print(f"  ⚠️ 选定值已越过正样本下界 —— 有 {len(victims)} 条正样本在**向量维**"
              f"被判为低置信：")
        for r in victims:
            print(f"       #{r['id']:<3} vec_top1={r['vec_top1']:.4f}  "
                  f"coverage={r['coverage']:.4f}   {r['query']}")
        print("     它们之所以没被误杀，**完全依赖覆盖率维度**（cov 远高于 cov_min）。")
        print("     → 覆盖率维度是主判据，向量维度在本语料上是冗余兜底。")
        print("     → 若将来把 cov_min 放宽，这些样本会立刻变成误杀。两阈值耦合，改一个要看另一个。")

    # --- 窗口宽度：向量维可用窗口有多窄 ---
    print()
    if vec_neg_max < vec_pos_min:
        win = vec_pos_min - vec_neg_max
        print(f"向量维可用窗口 = ({vec_neg_max:.4f}, {vec_pos_min:.4f})，宽度仅 {win:.4f}")
        print("  ⚠️ 窗口这么窄，说明**向量分本身无法承担相关性判定** —— 这与"
              "config.py「向量路无法区分相关/无关」的结论一致。")
        print("    因此 no_hit_score 只能当兜底，判别主力必须是覆盖率。")


def main():
    ap = argparse.ArgumentParser(description="无结果门控阈值标定")
    ap.add_argument("--mode", default="hybrid+rw",
                    help="评估模式，仅 hybrid / hybrid+rw 有意义（默认 hybrid+rw）")
    args = ap.parse_args()

    data = load_eval_set()
    queries = data["queries"]
    print(f"评估集：{len(queries)} 条"
          f"（正 {sum(1 for q in queries if q['expected_doc_ids'])} / "
          f"负 {sum(1 for q in queries if not q['expected_doc_ids'])}）"
          f"   模式 {args.mode}   top_k={RETRIEVE_TOP_K}")

    db = SessionLocal()
    try:
        rows = collect(db, queries, args.mode)
    finally:
        db.close()

    if any(r["degraded"] for r in rows):
        print()
        print("⚠️⚠️ 检测到降级（向量模型不可用）—— 此时 vec_top1 全为 0，")
        print("     标定结果无效！请先确认模型已加载再重跑。")

    print_distribution(rows)
    print_grid(rows)


if __name__ == "__main__":
    main()
