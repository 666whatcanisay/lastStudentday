#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
对库内已有文档**重新执行字段抽取**。

────────────────────────────────────────────────────────────────
为什么需要它
────────────────────────────────────────────────────────────────
抽取逻辑只在**上传/识别时**跑一次（`app/routers/ocr.py:83`），
结果落进 `document_extract` 表。因此改正则后：
  · 新上传的文档 —— 自动用新逻辑
  · **已在库里的文档 —— 仍是旧值，不会自动更新**

本项目没有迁移机制（只有 `create_all()`），所以需要一个补跑工具。
好在抽取只依赖 `document_info.ocr_raw_text`，**不必重跑 OCR**，
因此这个脚本是秒级的。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    .venv/Scripts/python.exe scripts/reextract_fields.py --dry-run   # 只预览，不写库
    .venv/Scripts/python.exe scripts/reextract_fields.py             # 实际写库
    .venv/Scripts/python.exe scripts/reextract_fields.py --doc-ids 6,7,8
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DATABASE_PATH  # noqa: E402
from app.services.extractor import FIELD_LABELS, extract_fields  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写库")
    ap.add_argument("--doc-ids", default="", help="逗号分隔的 doc_id；默认全部")
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

    docs = list(cur.execute(f"select id, file_name, ocr_raw_text from document_info {where} order by id", params))
    print(f"待处理文档：{len(docs)} 篇" + ("（dry-run，不写库）" if args.dry_run else ""))

    changed = 0
    for d in docs:
        old = {r["field_name"]: r["field_value"] for r in
               cur.execute("select field_name, field_value from document_extract where doc_id=?", (d["id"],))}
        new = {f["field_name"]: f["field_value"] for f in extract_fields(d["ocr_raw_text"])}

        diffs = [(k, old.get(k), new.get(k)) for k in new if old.get(k) != new.get(k)]
        if not diffs:
            continue
        changed += 1
        print(f"\n  doc{d['id']}  {d['file_name'][:34]}")
        for k, o, n in diffs:
            print(f"      {k:<6} {str(o):<18} → {n}")

        if not args.dry_run:
            cur.execute("delete from document_extract where doc_id=?", (d["id"],))
            for name, value in new.items():
                cur.execute(
                    "insert into document_extract (doc_id, field_name, field_value) values (?, ?, ?)",
                    (d["id"], name, value),
                )

    if not args.dry_run:
        conn.commit()
    print(f"\n{'将有' if args.dry_run else '已'}更新 {changed} / {len(docs)} 篇文档")
    if not args.dry_run and changed:
        print("  ⚠️ 抽取结果变了，前端详情页与导出会随之更新；RAG 检索不受影响"
              "（chunker 只吃 ocr_raw_text，不读抽取字段）")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
