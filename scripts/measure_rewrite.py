"""
查询改写效果实测脚本（阶段三）

目的：用真实语料量化「改写 vs 不改写」的差异，为 query_rewrite 模块
      「分路投喂」的设计决策提供数据依据，并定位残余问题。

三部分实验：
  一、分路对照   —— 分别看两条路在「原句 / 改写后」的排名变化。
                    这是最灵敏的观测点：改写只影响它该影响的那条路。
  二、融合结果   —— 看最终 Top-1 命中率。诚实报告融合后是否提升。
  三、权重敏感性 —— RRF 权重扫描，验证"向量路错误排序拖累融合"的猜想，
                    为阶段五的数据驱动调优给出方向。

用法：
  ./.venv/Scripts/python.exe scripts/measure_rewrite.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.services.query_rewrite import rewrite
from app.services.rag_pipeline import retrieve

# 期望命中的文档（依据真实语料人工标注）
#   1 = test_invoice.png（INV-20240115-001，服务器设备，2024-01-15）
#   2 = test_new.png（PO-20260724-888，2026-07-24）
#   5 = 曹操出行打车发票（2025年12月19日，客运服务费，¥25.99）
CASES = [
    ("上个月打车花了多少钱",            5, "口语词『打车』+ 相对时间"),
    ("税号是多少",                      5, "口语词『税号』"),
    ("去年12月19日的出行发票",          5, "相对年份 + 月日"),
    ("谁开的发票",                      5, "口语词『谁开的』"),
    ("这张发票的票号是多少",            5, "口语词『票号』"),
    ("从哪到哪",                        5, "口语词『从哪到哪』"),
    ("INV-20240115-001 这张发票的税额", 1, "精确编号（向量路弱项）"),
    ("服务器设备一批的单价",            1, "语义查询"),
]


def rank_of(docs, expect):
    """期望文档在结果中的名次（1-based）；未命中返回 '-'"""
    return docs.index(expect) + 1 if expect in docs else "-"


def main():
    db = SessionLocal()
    try:
        # ==================== 一、分路对照 ====================
        print("=" * 104)
        print("一、分路对照：查询改写对「各自那一路」的影响（名次越小越好，'-' 表示未召回）")
        print("=" * 104)
        print(f"{'查询':<30} {'期望':<5} {'BM25原句':<9} {'BM25扩展':<9} {'VEC原句':<8} {'VEC改写':<8} 说明")
        print("-" * 104)

        bm25_fixed = 0
        for query, expect, note in CASES:
            rw = rewrite(query)

            r_bo = retrieve(db, query=query, mode="bm25")
            r_bx = retrieve(db, query=query, mode="bm25", bm25_query=rw["bm25_query"])
            r_vo = retrieve(db, query=query, mode="vec")
            r_vx = retrieve(db, query=query, mode="vec", vec_query=rw["vec_query"])

            ro = rank_of([h["doc_id"] for h in r_bo["hits"]], expect)
            rx = rank_of([h["doc_id"] for h in r_bx["hits"]], expect)
            vo = rank_of([h["doc_id"] for h in r_vo["hits"]], expect)
            vx = rank_of([h["doc_id"] for h in r_vx["hits"]], expect)

            # 判定「关键词路被改写救回来」
            flag = ""
            if ro == "-" and rx != "-":
                flag = " ★改写救回"
                bm25_fixed += 1
            elif isinstance(ro, int) and isinstance(rx, int) and rx < ro:
                flag = " ▲名次提升"
                bm25_fixed += 1

            fmt = lambda v: str(v)
            print(f"{query:<30} doc{expect:<2} {fmt(ro):<9} {fmt(rx):<9} "
                  f"{fmt(vo):<8} {fmt(vx):<8} {note}{flag}")

        print("-" * 104)
        print(f"关键词路被改写改善的查询数：{bm25_fixed}/{len(CASES)}")

        # ==================== 二、融合结果 ====================
        print()
        print("=" * 104)
        print("二、融合结果：最终 Top-1 命中率（hybrid + RRF，权重取配置：vec=1.0 / bm25=1.5）")
        print("=" * 104)

        stat = {"A 原句双路": 0, "B 改写·仅关键词路": 0, "C 改写·双路": 0}
        for query, expect, note in CASES:
            rw = rewrite(query)
            variants = {
                "A 原句双路": None,
                "B 改写·仅关键词路": {**rw, "vec_query": rw["normalized"]},
                "C 改写·双路": rw,
            }
            line = []
            for name, cfg in variants.items():
                if cfg is None:
                    r = retrieve(db, query=query, mode="hybrid")
                else:
                    r = retrieve(db, query=query, mode="hybrid",
                                 vec_query=cfg["vec_query"], bm25_query=cfg["bm25_query"])
                docs = [h["doc_id"] for h in r["hits"]]
                hit = bool(docs) and docs[0] == expect
                stat[name] += int(hit)
                line.append(("✓" if hit else "✗") + str(docs[:3]))
            print(f"  {query:<30} 期望doc{expect}  " + "   ".join(
                f"{k.split()[0]}={v}" for k, v in zip(variants.keys(), line)))

        print()
        for k, v in stat.items():
            print(f"  {k:<20} Top-1 命中率 {v}/{len(CASES)}")

        # ==================== 三、RRF 权重敏感性 ====================
        print()
        print("=" * 104)
        print("三、权重敏感性：调高关键词路权重能否修复「向量路错误排序拖累融合」")
        print("=" * 104)
        from app.services.fusion import rrf_fuse
        from app.config import RRF_K

        for w_bm25 in (1.0, 1.5, 2.0, 3.0):
            correct = 0
            details = []
            for query, expect, _ in CASES:
                rw = rewrite(query)
                r_bo = retrieve(db, query=query, mode="vec", vec_query=rw["vec_query"])
                r_bx = retrieve(db, query=query, mode="bm25", bm25_query=rw["bm25_query"])
                fused = rrf_fuse(r_bo["hits"], r_bx["hits"], k=RRF_K,
                                 vec_weight=1.0, bm25_weight=w_bm25)
                docs = [h["doc_id"] for h in fused]
                if docs and docs[0] == expect:
                    correct += 1
                    details.append(f"doc{expect}✓")
                else:
                    details.append(f"doc{expect}✗({docs[:1]})")
            print(f"  bm25_weight={w_bm25:<4} Top-1 命中率 {correct}/{len(CASES)}   " + " ".join(details))

        print()
        print("结论见 RAG_WORKFLOW.md「查询改写分路实证」小节。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
