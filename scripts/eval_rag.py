"""
阶段五：RAG 检索评估脚本（Recall@K / MRR / 负样本判定正确率）

只评估**检索层**，不调 LLM —— 因此无需配置 LLM_API_KEY 即可运行。

用法：
  # 基线：vec / bm25 / hybrid 三种模式对比
  ./.venv/Scripts/python.exe scripts/eval_rag.py

  # 逐条明细（标签分歧排查用）
  ./.venv/Scripts/python.exe scripts/eval_rag.py --verbose

  # 参数扫描（融合权重 / 无结果阈值）
  ./.venv/Scripts/python.exe scripts/eval_rag.py --sweep

  # 机器可读输出
  ./.venv/Scripts/python.exe scripts/eval_rag.py --json

设计说明（为什么长这样）：
  1. 评估集是**数据**不是代码，放在 scripts/eval_set.json，便于持续扩充。
  2. 正样本与负样本**分开报指标**。正样本看 Recall/MRR，负样本看"判为无结果"的
     正确率 —— 后者才是 RETRIEVE_NO_HIT_SCORE 的验证手段。
  3. 无结果门控只在 hybrid 生效（见 rag_pipeline.retrieve 注释），
     所以负样本正确率只在 hybrid 行有意义，单路行标注 n/a。
  4. --sweep 会临时注入融合权重（retrieve() 未开放该参数）。**扫描结果只用于选值，
     定稿改的是 app/config.py 的默认值，脚本里不保留任何 monkeypatch。**
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台默认 GBK，中文指标名会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.config import (  # noqa: E402
    FUSION_WEIGHT_BM25,
    RETRIEVE_NO_HIT_SCORE,
    RETRIEVE_SCORE_MARGIN,
    RETRIEVE_TOP_K,
)
from app.database import SessionLocal  # noqa: E402
import app.services.rag_pipeline as rp  # noqa: E402
from app.services.bm25_index import get_bm25_index  # noqa: E402
from app.services.query_rewrite import rewrite as rewrite_query  # noqa: E402
from app.services.rag_indexer import ensure_bm25_ready  # noqa: E402
from app.services.rag_pipeline import retrieve  # noqa: E402

EVAL_MODES = ("vec", "bm25", "bm25+rw", "hybrid", "hybrid+rw")

EVAL_SET_PATH = Path(__file__).resolve().parent / "eval_set.json"
K_LIST = (1, 3, 5)


# ==================== 评估集 ====================

def load_eval_set() -> Dict:
    with open(EVAL_SET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ==================== 单次运行 ====================

# 四种被评估的模式。前三种是**检索层**的对比视图，
# 第四种 "pipeline" 才等于生产链路 —— 它先过 query_rewrite（纯规则，不需要 LLM Key），
# 再把 vec_query / bm25_query **分路投喂**。
#
# 为什么必须把 pipeline 单列出来：阶段三的核心设计就是"分路投喂"
# （向量路用归一化原句，关键词路拼中文日期 + 同义扩展）。只测 retrieve()
# 等于把改写这一步整个跳过了，日期/金额/口语类查询的成绩会被系统性低估。
MODE_LABELS = {
    "vec": "vec（仅向量）",
    "bm25": "bm25（仅关键词）",
    "hybrid": "hybrid（双路，原句）",
    "bm25+rw": "bm25+改写",
    "hybrid+rw": "hybrid+改写（生产链路）",
}


def run_one(db, q: Dict, mode: str, top_k: int,
            no_hit_score: Optional[float] = None,
            score_margin: Optional[float] = None) -> Dict:
    """跑一条 query，返回 {doc_ids, no_hit, vec_count, bm25_count, vec_top1, degraded}

    mode 支持 "+rw" 后缀：加了就先过 query_rewrite 再分路投喂。
    把改写拆成独立维度（而不是只做一个 pipeline 模式）是为了能**单独观察
    改写对关键词路的贡献** —— 混合模式下向量路常常已经兜住，改写的作用被掩盖。
    """
    base_mode = mode.replace("+rw", "")
    # 显式校验：retrieve() 对未知 mode 是**静默返回空结果**（两路都不跑），
    # 表现为"全部判无结果"。这类错误没有异常、只有诡异的全 0 指标，
    # 曾经因为一处 mode 串写错而把整张扫描表变成 0%。这里直接拦掉。
    if base_mode not in ("vec", "bm25", "hybrid"):
        raise ValueError(f"未知 mode: {mode!r}（base={base_mode!r}）")

    kwargs = {"mode": base_mode, "top_k": top_k}

    if mode.endswith("+rw"):
        # rewrite() 默认 use_llm=False，纯规则，不需要 LLM Key
        rw = rewrite_query(q["query"])
        kwargs["vec_query"] = rw.get("vec_query") or q["query"]
        kwargs["bm25_query"] = rw.get("bm25_query") or q["query"]

    if no_hit_score is not None:
        kwargs["no_hit_score"] = no_hit_score
    if score_margin is not None:
        kwargs["score_margin"] = score_margin

    r = retrieve(db, query=q["query"], **kwargs)

    # BM25 的 top1 原始分。retrieve() 不返回它，但它是诊断无结果门控的必需品：
    # 现行门控要求「bm25 零命中」，只要查询含常见词（公司/金额/日期）就必然命中，
    # 门控直接被关掉。要判断这是不是结构性问题，必须看到分数有多低。
    #
    # ⚠️ 必须用**改写后**的 bm25_query 来测，不能再用原句 —— 生产链路是
    #    rewrite() → 分路投喂，用原句测出来的分数与线上不是同一个东西。
    bm25_probe = kwargs.get("bm25_query") or q["query"]
    bm25_top1 = 0.0
    if ensure_bm25_ready(db):
        bh = get_bm25_index().search(bm25_probe, top_n=1)
        if bh:
            bm25_top1 = bh[0]["score"]

    return {
        "doc_ids": [h.get("doc_id") for h in r["hits"]],
        "no_hit": r["no_hit"],
        "vec_count": r["vec_count"],
        "bm25_count": r["bm25_count"],
        "vec_top1": r["vec_top1"],
        "bm25_top1": bm25_top1,
        "degraded": r["degraded"],
    }


def first_relevant_rank(doc_ids: List[int], expected: List[int]) -> int:
    """返回第一个命中期望文档的位次（1-based）；未命中返回 0。"""
    for i, d in enumerate(doc_ids, start=1):
        if d in expected:
            return i
    return 0


def evaluate(db, queries: List[Dict], mode: str, top_k: int,
             no_hit_score: Optional[float] = None,
             score_margin: Optional[float] = None) -> Dict:
    """对评估集整体求指标。"""
    pos = [q for q in queries if q["expected_doc_ids"]]
    neg = [q for q in queries if not q["expected_doc_ids"]]

    recall_hits = {k: 0 for k in K_LIST}
    rr_sum = 0.0
    pos_details = []
    neg_details = []
    degraded_any = False

    for q in pos:
        res = run_one(db, q, mode, top_k, no_hit_score, score_margin)
        degraded_any = degraded_any or res["degraded"]
        rank = first_relevant_rank(res["doc_ids"], q["expected_doc_ids"])
        rr_sum += (1.0 / rank) if rank else 0.0
        for k in K_LIST:
            # 期望文档出现在前 k 位之内即算命中
            if any(d in q["expected_doc_ids"] for d in res["doc_ids"][:k]):
                recall_hits[k] += 1
        pos_details.append({
            "id": q["id"], "query": q["query"], "category": q["category"],
            "expected": q["expected_doc_ids"], "got": res["doc_ids"],
            "rank": rank, "no_hit": res["no_hit"],
            "vec_top1": res["vec_top1"], "bm25_top1": res["bm25_top1"],
            "vec_count": res["vec_count"], "bm25_count": res["bm25_count"],
        })

    for q in neg:
        res = run_one(db, q, mode, top_k, no_hit_score, score_margin)
        degraded_any = degraded_any or res["degraded"]
        # 单路模式不套用无结果门控（见 retrieve 注释），此时只看是否召回为空
        judged_no_hit = res["no_hit"]
        neg_details.append({
            "id": q["id"], "query": q["query"],
            "judged_no_hit": judged_no_hit, "got": res["doc_ids"],
            # vec_top1 是诊断 no_hit_score 是否合理的**唯一直接证据**：
            # 它离阈值多远，决定调阈值是"擦边翻转"还是"有明显余量"。
            "vec_top1": res["vec_top1"], "bm25_top1": res["bm25_top1"],
            "vec_count": res["vec_count"], "bm25_count": res["bm25_count"],
        })

    n_pos, n_neg = len(pos), len(neg)
    return {
        "mode": mode,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "recall": {f"@{k}": (recall_hits[k] / n_pos if n_pos else 0.0) for k in K_LIST},
        "mrr": (rr_sum / n_pos) if n_pos else 0.0,
        "neg_acc": (sum(1 for d in neg_details if d["judged_no_hit"]) / n_neg) if n_neg else None,
        "degraded": degraded_any,
        "pos_details": pos_details,
        "neg_details": neg_details,
    }


# ==================== 展示 ====================

def fmt_pct(x: Optional[float]) -> str:
    return "  n/a " if x is None else f"{x * 100:5.1f}%"


def dwidth(s: str) -> int:
    """字符串在终端里的显示宽度。CJK 字符占 2 列，
    直接拿 len() 去对齐会让中文表头整体错位。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def pad(s: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    return s + " " * max(0, width - dwidth(s))


def rpad(s: str, width: int) -> str:
    """按显示宽度右对齐补空格（表头列名要对齐右对齐的数字列）。"""
    return " " * max(0, width - dwidth(s)) + s


def print_baseline(results: List[Dict], queries: List[Dict]):
    print()
    print("=" * 88)
    print("检索评估基线 —— Recall@K / MRR / 负样本判定正确率")
    print("=" * 88)
    n_pos = results[0]["n_pos"]
    n_neg = results[0]["n_neg"]
    print(f"评估集：{len(queries)} 条（正样本 {n_pos} 条，负样本 {n_neg} 条）"
          f"   top_k={RETRIEVE_TOP_K}   score_margin={RETRIEVE_SCORE_MARGIN}"
          f"   no_hit_score={RETRIEVE_NO_HIT_SCORE}")
    print()
    header = (pad("模式", 26)
              + f"{'Recall@1':>10}{'Recall@3':>10}{'Recall@5':>10}{'MRR':>9}"
              + rpad("负样本正确率", 14))
    print(header)
    print("-" * dwidth(header))
    for r in results:
        print(pad(MODE_LABELS.get(r["mode"], r["mode"]), 26)
              + f"{fmt_pct(r['recall']['@1']):>10}"
              f"{fmt_pct(r['recall']['@3']):>10}"
              f"{fmt_pct(r['recall']['@5']):>10}"
              f"{r['mrr']:>9.4f}"
              f"{fmt_pct(r['neg_acc']):>14}"
              + ("   [降级]" if r["degraded"] else ""))
    print()
    print("说明 1：负样本正确率 = 判为「无结果」的比例。无结果门控只在 hybrid 生效，")
    print("        单路模式（vec/bm25）是诊断视图，不替用户下结论，该列仅供参考。")
    print("说明 2：bm25 行与 bm25+改写 行的差 = **改写对关键词路的净贡献**；")
    print("        hybrid 行与 hybrid+改写 行的差 = 改写在双路下的**增量**贡献。")
    print("        二者不等时，说明向量路已兜住改写补回来的那条 —— 改写不是没用，")
    print("        而是被冗余覆盖了；一旦向量路降级（模型不可用→BM25 单路），")
    print("        改写就变成唯一依靠。所以该列要结合降级场景一起看。")


def print_verbose(results: List[Dict]):
    """逐条明细。vec_top1 与 vec/bm25 原始召回数是**标签分歧排查的关键证据**：
    只有知道向量分和两路命中数，才能判断某条是"检索真错"还是"标签定错"。"""
    for r in results:
        print()
        print("=" * 104)
        print(f"明细 —— 模式 {r['mode']}")
        print("=" * 104)
        print(f"{'#':>3} {'类别':<10}{'期望':<12}{'实际':<22}{'位次':>5}"
              f"{'vec_top1':>10}{'bm25_top1':>11}{'v/b':>7}  判定  query")
        print("-" * 112)
        for d in r["pos_details"]:
            flag = "OK  " if d["rank"] else "MISS"
            counts = f"{d['vec_count']}/{d['bm25_count']}"
            print(f"{d['id']:>3} {d['category']:<10}{str(d['expected']):<12}"
                  f"{str(d['got'])[:20]:<22}{d['rank']:>5}"
                  f"{d['vec_top1']:>10.4f}{d['bm25_top1']:>11.3f}{counts:>7}"
                  f"  {flag} {d['query']}")
        for d in r["neg_details"]:
            flag = "OK  " if d["judged_no_hit"] else "FP  "
            counts = f"{d['vec_count']}/{d['bm25_count']}"
            print(f"{d['id']:>3} {'negative':<10}{'[]':<12}"
                  f"{str(d['got'])[:20]:<22}{'-':>5}"
                  f"{d['vec_top1']:>10.4f}{d['bm25_top1']:>11.3f}{counts:>7}"
                  f"  {flag} {d['query']}")


def print_rewrite_diag(queries: List[Dict]):
    """打印每条 query 经 query_rewrite 后的**分路投喂实际输入**。

    这是回答「改写到底有没有起作用」的唯一直接证据 ——
    指标相同既可能是"改写没带来增益"，也可能是"改写根本没改动输入"，
    两者含义完全不同，必须看输入才能区分。
    """
    print()
    print("=" * 104)
    print("查询改写诊断 —— 分路投喂的实际输入")
    print("=" * 104)
    changed = 0
    for q in queries:
        rw = rewrite_query(q["query"])
        vq = rw.get("vec_query") or q["query"]
        bq = rw.get("bm25_query") or q["query"]
        if vq != q["query"] or bq != q["query"]:
            changed += 1
        print()
        print(f"[{q['id']:>2}] {q['query']}")
        print(f"     method={rw.get('method')}  time_scope={rw.get('time_scope')}")
        print(f"     向量路输入: {vq}")
        print(f"     关键词路输入: {bq}")
    print()
    print(f"→ {changed}/{len(queries)} 条 query 的改写输入与原句不同")
    print("  （若比例很低，说明评估集本身没覆盖改写要解决的场景 —— 是评估集的缺陷，")
    print("    不能据此判定改写无用）")


# ==================== 参数扫描 ====================

def with_bm25_weight(w: float):
    """临时注入 BM25 权重（retrieve() 未开放该参数）。返回还原函数。"""
    orig = rp.fuse

    def patched(vec_hits, bm25_hits, mode="rrf", **kw):
        kw["bm25_weight"] = w
        return orig(vec_hits, bm25_hits, mode=mode, **kw)

    rp.fuse = patched
    return lambda: setattr(rp, "fuse", orig)


def run_sweep(db, queries: List[Dict], top_k: int):
    print()
    print("=" * 88)
    print("参数扫描 A —— 融合权重 FUSION_WEIGHT_BM25（生产链路 / RRF）")
    print("=" * 88)
    print(f"当前配置值：{FUSION_WEIGHT_BM25}")
    print()
    hdr = f"{'bm25_weight':>12}{'Recall@1':>10}{'Recall@3':>10}{'Recall@5':>10}{'MRR':>9}{'负样本正确率':>14}"
    print(hdr)
    print("-" * len(hdr))
    best = None
    for w in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0):
        restore = with_bm25_weight(w)
        try:
            r = evaluate(db, queries, "hybrid+rw", top_k)
        finally:
            restore()
        mark = "  ← 当前值" if abs(w - FUSION_WEIGHT_BM25) < 1e-9 else ""
        print(f"{w:>12.1f}"
              f"{fmt_pct(r['recall']['@1']):>10}"
              f"{fmt_pct(r['recall']['@3']):>10}"
              f"{fmt_pct(r['recall']['@5']):>10}"
              f"{r['mrr']:>9.4f}"
              f"{fmt_pct(r['neg_acc']):>14}{mark}")
        if best is None or r["mrr"] > best[1]:
            best = (w, r["mrr"])
    print()
    print(f"→ MRR 最优权重：{best[0]}（MRR={best[1]:.4f}）")

    print()
    print("=" * 88)
    print("参数扫描 B —— 无结果判定阈值 RETRIEVE_NO_HIT_SCORE（生产链路）")
    print("=" * 88)
    print(f"当前配置值：{RETRIEVE_NO_HIT_SCORE}")
    print()
    print(hdr.replace("bm25_weight", "no_hit_score"))
    print("-" * len(hdr))
    for s in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        r = evaluate(db, queries, "hybrid+rw", top_k, no_hit_score=s)
        mark = "  ← 当前值" if abs(s - RETRIEVE_NO_HIT_SCORE) < 1e-9 else ""
        print(f"{s:>12.2f}"
              f"{fmt_pct(r['recall']['@1']):>10}"
              f"{fmt_pct(r['recall']['@3']):>10}"
              f"{fmt_pct(r['recall']['@5']):>10}"
              f"{r['mrr']:>9.4f}"
              f"{fmt_pct(r['neg_acc']):>14}{mark}")
    print()
    print("说明：阈值越高越容易判「无结果」——负样本正确率上升，但正样本召回可能下降。")
    print("      要看的是这条权衡曲线的拐点，而不是单看某一列。")


# ==================== main ====================

def main():
    ap = argparse.ArgumentParser(description="RAG 检索评估（Recall@K / MRR）")
    ap.add_argument("--verbose", action="store_true", help="逐条明细")
    ap.add_argument("--rewrite", action="store_true", help="打印查询改写的分路输入")
    ap.add_argument("--sweep", action="store_true", help="参数扫描")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--top-k", type=int, default=RETRIEVE_TOP_K)
    args = ap.parse_args()

    data = load_eval_set()
    queries = data["queries"]

    db = SessionLocal()
    try:
        results = [evaluate(db, queries, m, args.top_k) for m in EVAL_MODES]

        if args.json:
            slim = [{k: v for k, v in r.items()
                     if k not in ("pos_details", "neg_details")} for r in results]
            print(json.dumps({"results": slim, "queries": len(queries)},
                             ensure_ascii=False, indent=2))
        else:
            print(f"\n评估集：{EVAL_SET_PATH.name}")
            print(f"语料说明：{data.get('_corpus_snapshot', '')}")
            if data.get("_caveat"):
                print(f"\n{data['_caveat']}")
            print_baseline(results, queries)
            if args.verbose:
                print_verbose(results)
            if args.rewrite:
                print_rewrite_diag(queries)
            if args.sweep:
                run_sweep(db, queries, args.top_k)
    finally:
        db.close()


if __name__ == "__main__":
    main()
