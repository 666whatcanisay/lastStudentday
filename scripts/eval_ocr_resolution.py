#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分辨率档位对比 —— 报告二「对比项 B」的评测脚本。

────────────────────────────────────────────────────────────────
为什么要单独测分辨率（而不是直接跑完整流水线）
────────────────────────────────────────────────────────────────
`preprocess.py` 会把长边压到 `MAX_SIDE = 1400`。如果渲染 DPI 过高，
降采样会把多出来的像素**全部丢掉** —— 也就是「渲染更高 DPI 白费」。
所以本脚本**刻意绕过 preprocess**，把渲染原图直接喂给 OCR，
才能观察到分辨率的净影响。
最后另附一档「300 DPI + 预处理」作为对照，用来验证上面这个判断是否成立。

────────────────────────────────────────────────────────────────
真值来源
────────────────────────────────────────────────────────────────
**PDF 文本层**（`fitz` 读取），不是重跑 `gen_corpus.py`。
原因：`gen_corpus.py:259` 用 `random.Random(hash(spec["no"]) & 0xFFFF)` 播种，
而 Python 的字符串 `hash()` 带随机盐 —— 实测同一编号跨进程得到 16927 / 49750，
**语料不可原样重生成**。文本层才是这批 PDF 的唯一稳定真值。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    .venv/Scripts/python.exe scripts/eval_ocr_resolution.py
    .venv/Scripts/python.exe scripts/eval_ocr_resolution.py --dpis 100,200,300
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import unicodedata
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.ocr_engine import get_ocr_engine, recognize  # noqa: E402
from app.services.pdf_handler import pdf_to_images  # noqa: E402
from app.services.preprocess import preprocess_image  # noqa: E402

CORPUS = ROOT / "test_corpus"
DEFAULT_DPIS = (100, 150, 200, 300)


# ---------------------------------------------------------------
# 文本归一化 —— **口径必须写进报告**，否则数字不可比
# ---------------------------------------------------------------
def norm(s: str) -> str:
    s = re.sub(r"===\s*第\s*\d+\s*页\s*===", "", s or "")   # 去掉分页标记
    s = unicodedata.normalize("NFKC", s)                     # 全角 → 半角
    return re.sub(r"\s+", "", s)                             # 去掉所有空白


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(gt: str, hyp: str) -> float:
    return levenshtein(gt, hyp) / max(len(gt), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpis", default=",".join(map(str, DEFAULT_DPIS)))
    ap.add_argument("--warmup", type=int, default=2,
                    help="预热篇数（首次调用含模型加载，不预热会把假象算进均值）")
    args = ap.parse_args()
    dpis = [int(x) for x in args.dpis.split(",") if x.strip()]

    pdfs = sorted(CORPUS.glob("*.pdf"))
    if not pdfs:
        print(f"未找到语料：{CORPUS}")
        return 1

    engine_type, _ = get_ocr_engine()
    print(f"OCR 引擎   : {engine_type}")
    print(f"语料       : {len(pdfs)} 篇  ({CORPUS})")
    print(f"分辨率档位 : {dpis} DPI")
    print(f"真值来源   : PDF 文本层（NFKC + 去空白）")
    print()

    # 真值预读
    truths = {}
    for p in pdfs:
        truths[p.name] = norm("".join(pg.get_text() for pg in fitz.open(p)))

    # 预热：首次调用含 ONNX session 初始化，不预热会严重高估耗时
    for p in pdfs[: args.warmup]:
        recognize(pdf_to_images(str(p), dpi=200)[0])
    print(f"已预热 {args.warmup} 篇\n")

    # rows[dpi][file] = (cer, cost_ms, w, h)
    rows: dict[int, dict[str, tuple]] = {d: {} for d in dpis}
    extra: dict[str, tuple] = {}          # 300 DPI + 预处理 对照组

    for dpi in dpis:
        print(f"── {dpi} DPI ──────────────────────────────")
        for p in pdfs:
            img = pdf_to_images(str(p), dpi=dpi)[0]
            h, w = img.shape[:2]
            text, cost, _ = recognize(img)
            c = cer(truths[p.name], norm(text))
            rows[dpi][p.name] = (c, cost, w, h)
            print(f"  {p.name[:26]:<28} {w}x{h:<6} {cost:>8.0f}ms  准确率 {1 - c:>6.1%}")

    if 300 in dpis:
        print(f"── 300 DPI + 预处理（对照组）─────────────")
        for p in pdfs:
            img = pdf_to_images(str(p), dpi=300)[0]
            img = preprocess_image(img)
            h, w = img.shape[:2]
            text, cost, _ = recognize(img)
            c = cer(truths[p.name], norm(text))
            extra[p.name] = (c, cost, w, h)
            print(f"  {p.name[:26]:<28} {w}x{h:<6} {cost:>8.0f}ms  准确率 {1 - c:>6.1%}")

    # ---------------- 汇总 ----------------
    def summarize(d: dict) -> tuple:
        cers = [v[0] for v in d.values()]
        costs = [v[1] for v in d.values()]
        px = [v[2] * v[3] for v in d.values()]
        return (statistics.mean(cers), statistics.median(costs), statistics.mean(px))

    print("\n" + "=" * 78)
    print("汇总（准确率 = 22 篇逐篇准确率的算术平均；耗时 = 中位数）")
    print("=" * 78)
    print(f"{'档位':<26}{'平均像素':>12}{'耗时中位数':>14}{'字符准确率':>12}")
    print("-" * 78)
    base_cost = None
    for dpi in dpis:
        m_cer, m_cost, m_px = summarize(rows[dpi])
        if base_cost is None:
            base_cost = m_cost
        ratio = m_cost / base_cost if base_cost else 1.0
        print(f"{dpi} DPI{'':<20}{m_px:>12,.0f}{m_cost:>11.0f}ms{1 - m_cer:>11.1%}")
    if extra:
        m_cer, m_cost, m_px = summarize(extra)
        print(f"{'300 DPI + 预处理':<26}{m_px:>12,.0f}{m_cost:>11.0f}ms{1 - m_cer:>11.1%}")

    print("\n【判断】预处理是否吃掉了高 DPI 的收益")
    if extra:
        m_px_300 = summarize(rows[300])[2]
        m_px_pre = summarize(extra)[2]
        print(f"  300 DPI 原图平均像素 {m_px_300:,.0f} → 预处理后 {m_px_pre:,.0f}"
              f"（降到 {m_px_pre / m_px_300:.0%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
