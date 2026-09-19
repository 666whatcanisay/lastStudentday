#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCR **字段抽取准确率**评测（单据编号 / 日期 / 金额）。

────────────────────────────────────────────────────────────────
被测对象与真值来源
────────────────────────────────────────────────────────────────
· 被测 = 库里 `document_info.ocr_raw_text`（**生产链路的真实 OCR 输出**）
         经 `app.services.extractor.extract_fields()` 抽取的结果。
         即本脚本测的是「端到端」链路，输入是带真实 OCR 噪声的文本，不是理想文本。
· 真值 = `test_corpus/*.pdf` 的 **PDF 文本层**（由 gen_corpus.py 程序生成，文本层即印刷真值）。

⚠️ **真值不可重跑 `gen_corpus.py` 获取**
   该脚本用 `random.Random(hash(spec["no"]) & 0xFFFF)` 播种，
   而 Python 字符串 `hash()` **带随机盐** —— 实测同一编号跨进程得 16927 / 49750。
   金额与明细行都是 RNG 生成的 → **语料无法原样重生成**。只有 PDF 文本层是稳定真值。

────────────────────────────────────────────────────────────────
金额字段的两种判定口径（**引用数字时必须说明用的是哪一种**）
────────────────────────────────────────────────────────────────
· **业务口径**（默认）：抽到的就是**单据的最终总金额**（价税合计 > 金额合计/合计金额）
· 宽松口径：抽到的数值是文档中**真实存在的某个金额**（可能是明细行、也可能是不含税合计）
  → 宽松口径会把「抽到明细行」「抽到不含税合计而非价税合计」判为正确，**掩盖真实缺陷**。
    报告采用业务口径。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    .venv/Scripts/python.exe scripts/eval_ocr_fields.py            # 业务口径汇总
    .venv/Scripts/python.exe scripts/eval_ocr_fields.py --verbose  # 逐篇明细 + 错例
    .venv/Scripts/python.exe scripts/eval_ocr_fields.py --loose    # 附宽松口径
    .venv/Scripts/python.exe scripts/eval_ocr_fields.py --source pdf  # 直接读 PDF 文本层当输入
                                                                      # （隔离「抽取层」误差，
                                                                      #   不跑 OCR，快）
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from app.config import DATABASE_PATH  # noqa: E402
from app.services.extractor import extract_fields  # noqa: E402

CORPUS_DIR = ROOT / "test_corpus"

# 真值金额优先级：**价税合计**（最终应付）> **金额合计 / 合计金额**
# ⚠️ 语料里两种词序**并存**：服务类发票用「合计金额」，采购/合同/报销/台账类用「金额合计」，
#    只写一种会漏掉一半文档的真值。
AMOUNT_TRUTH_PATTERNS = [
    r"价税合计[：:]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    r"(?:金额合计|合计金额|总金额)[：:]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
]

# 宽松口径的「金额候选」形态：**千分位金额** 或 **带「元」后缀的数值**。
#
# ⚠️ 不能用「文档中出现的所有数值」—— 那会把**数量列**也算成金额候选，
#    抓错的数量值（12 / 109 / 40 / 53 / 60 …）会被判为「是文档里的真实金额」，
#    宽松口径虚高到 86.4%（19/22），完全失去区分意义。
#    实测：用下面这个定义才能复现报告里的 63.6%（14/22）。
AMOUNT_LIKE_RE = re.compile(r"\d{1,3}(?:,\d{3})+\.\d{2}|\d+(?:\.\d{1,2})?\s*元")


def num_key(s) -> str | None:
    """**数值等价键** —— 比对金额必须用它，不能用字符串。

    OCR 常把千分位逗号丢掉（真值 `73,786.81` → 识别成 `73786.81`），
    字符串比对会把这种**正确**结果误判为错。归一化为定长小数串后比较。
    """
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", str(s)).strip()
    s = re.sub(r"[,\s]", "", s)
    if not s:
        return None
    try:
        return f"{float(s):.2f}"
    except ValueError:
        return s


def norm_text(s) -> str:
    """文本字段归一化：NFKC 全角转半角 + 去所有空白。"""
    if s is None:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s)))


def pdf_truth(pdf: Path) -> dict:
    """从 PDF 文本层提取真值。"""
    doc = fitz.open(str(pdf))
    text = "\n".join(p.get_text() for p in doc)
    doc.close()

    truth: dict = {"单据编号": None, "日期": None, "金额": None}
    m = re.search(r"单据编号[：:]\s*([^\s]+)", text)
    if m:
        truth["单据编号"] = m.group(1)
    m = re.search(r"日期[：:]\s*([^\s]+)", text)
    if m:
        truth["日期"] = m.group(1)
    for pat in AMOUNT_TRUTH_PATTERNS:
        m = re.search(pat, text)
        if m:
            truth["金额"] = m.group(1)
            break
    # 宽松口径用：文档中出现的**金额形态**数值（千分位 / 带「元」后缀）
    truth["_all"] = {num_key(x.rstrip("元").strip()) for x in AMOUNT_LIKE_RE.findall(text)}
    return truth


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="逐篇明细")
    ap.add_argument("--loose", action="store_true", help="同时输出宽松口径金额准确率")
    ap.add_argument("--source", choices=["db", "pdf"], default="db",
                    help="db=库内 ocr_raw_text（端到端，默认）；pdf=PDF 文本层（只测抽取层）")
    ap.add_argument("--doc-ids", default="", help="逗号分隔的 doc_id，仅 --source db 有效")
    args = ap.parse_args()

    if not Path(DATABASE_PATH).exists():
        print(f"数据库不存在：{DATABASE_PATH}")
        return 1

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = "where ocr_raw_text is not null and length(ocr_raw_text) > 0"
    params: list = []
    if args.doc_ids.strip():
        ids = [int(x) for x in args.doc_ids.split(",") if x.strip()]
        where += f" and id in ({','.join('?' * len(ids))})"
        params = ids

    docs = [d for d in cur.execute(
        f"select id, file_name, ocr_raw_text from document_info {where} order by id", params)
        if (CORPUS_DIR / d["file_name"]).exists()]

    if not docs:
        print("没有匹配到 test_corpus/ 下的文档")
        return 1

    print(f"样本：{len(docs)} 篇（真值 = PDF 文本层；输入 = "
          f"{'库内 ocr_raw_text（端到端）' if args.source == 'db' else 'PDF 文本层（仅抽取层）'}）")

    stat = {"单据编号": 0, "日期": 0, "金额": 0}
    stat_loose = 0
    rows = []

    for d in docs:
        truth = pdf_truth(CORPUS_DIR / d["file_name"])
        raw = d["ocr_raw_text"] if args.source == "db" else \
            "\n".join(p.get_text() for p in fitz.open(str(CORPUS_DIR / d["file_name"])))
        fields = {f["field_name"]: f["field_value"] for f in extract_fields(raw)}

        ok = {}
        ok["单据编号"] = norm_text(fields.get("单据编号")) == norm_text(truth["单据编号"]) \
            and truth["单据编号"] is not None
        ok["日期"] = norm_text(fields.get("日期")) == norm_text(truth["日期"]) \
            and truth["日期"] is not None
        got_amount = num_key(fields.get("金额"))
        ok["金额"] = got_amount is not None and got_amount == num_key(truth["金额"])
        loose_ok = got_amount is not None and got_amount in truth["_all"]

        for k in stat:
            stat[k] += 1 if ok[k] else 0
        stat_loose += 1 if loose_ok else 0

        rows.append((d["id"], d["file_name"], truth, fields, ok, loose_ok))

    n = len(rows)

    if args.verbose:
        print()
        print(f"{'文档':<30} {'编号':^5} {'日期':^5} {'金额':^5}  抽取金额 / 真值")
        print("-" * 92)
        for _id, name, truth, fields, ok, _l in rows:
            mark = lambda b: " ✅ " if b else " ❌ "
            print(f"{name[:28]:<30} {mark(ok['单据编号'])} {mark(ok['日期'])} {mark(ok['金额'])}  "
                  f"{str(fields.get('金额')):<14} / {truth['金额']}")
        print()

    print("── 准确率 ──")
    for k in ("单据编号", "日期", "金额"):
        label = f"{k}（业务口径）" if k == "金额" else k
        print(f"  {label:<16} {stat[k]:>2} / {n}  = {stat[k] / n * 100:.1f}%")
    if args.loose:
        print(f"  {'金额（宽松口径）':<16} {stat_loose:>2} / {n}  = {stat_loose / n * 100:.1f}%")

    # 错例清单
    bad = [r for r in rows if not r[4]["金额"]]
    if bad:
        print(f"\n── 金额错例（{len(bad)} 篇）──")
        for _id, name, truth, fields, _ok, loose in bad:
            tag = "（宽松口径判对，业务口径判错：抽到的是非最终总金额）" if loose else "（完全抽错）"
            print(f"  {name[:28]:<30} 抽到 {str(fields.get('金额')):<14} 真值 {truth['金额']}  {tag}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
