#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
语料入库脚本 —— 把 test_corpus/ 里的 PDF 走**真实 OCR 链路**灌进主库。

────────────────────────────────────────────────────────────────
流程（每篇）
────────────────────────────────────────────────────────────────
    POST /api/file/upload      → 落盘 + 建 DocumentInfo（status=0）
    POST /api/ocr/recognize    → 真实 RapidOCR 识别 + 字段抽取（status=1）
最后再 POST /api/rag/index 重建检索索引。

刻意**不绕过 OCR 直接写数据库** —— 评估要衡量的是端到端真实链路，
包括 OCR 的识别误差。手工塞干净文本会让评估结果偏乐观。

────────────────────────────────────────────────────────────────
用法
────────────────────────────────────────────────────────────────
    python scripts/ingest_corpus.py                       # 入库 test_corpus/
    python scripts/ingest_corpus.py --dir D:/tmp/corpus
    python scripts/ingest_corpus.py --skip-existing       # 按文件名跳过已入库的
    python scripts/ingest_corpus.py --no-index            # 只入库，不重建索引

产物：<dir>/_ingest_map.json —— 文件名 → doc_id 的映射，
      供 scripts/eval_set.json 引用具体文档 ID。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
# OCR 是逐页推理的，单篇可能几秒；给足超时避免误判失败
UPLOAD_TIMEOUT = 120.0
OCR_TIMEOUT = 600.0


def _get(client: httpx.Client, path: str) -> dict:
    r = client.get(BASE + path, timeout=60.0)
    r.raise_for_status()
    return r.json()


def existing_names(client: httpx.Client) -> set[str]:
    """已入库的文件名集合（用于 --skip-existing）。"""
    names: set[str] = set()
    page = 1
    while True:
        d = _get(client, f"/api/document/list?page={page}&page_size=100")
        data = d.get("data") or {}
        items = data.get("items") or []
        for it in items:
            if it.get("file_name"):
                names.add(it["file_name"])
        if len(items) < 100:
            break
        page += 1
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description="把测试语料走真实 OCR 链路灌进主库")
    ap.add_argument("--dir", type=Path,
                    default=Path(__file__).resolve().parent.parent / "test_corpus")
    ap.add_argument("--skip-existing", action="store_true",
                    help="按文件名跳过已入库的文档")
    ap.add_argument("--no-index", action="store_true",
                    help="入库后不重建 RAG 索引")
    args = ap.parse_args()

    if not args.dir.exists():
        print(f"目录不存在: {args.dir}\n先跑 python scripts/gen_corpus.py", file=sys.stderr)
        return 1

    pdfs = sorted(args.dir.glob("*.pdf"))
    if not pdfs:
        print(f"{args.dir} 下没有 PDF", file=sys.stderr)
        return 1

    mapping_path = args.dir / "_ingest_map.json"
    mapping: dict[str, dict] = {}
    if mapping_path.exists():
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except Exception:
            mapping = {}

    with httpx.Client(trust_env=False) as client:
        # 先探活，避免后面逐个失败才发现服务没起
        try:
            h = _get(client, "/api/health")
        except Exception as e:
            print(f"后端不可用（{BASE}）：{type(e).__name__}: {e}\n"
                  f"请先启动：.venv/Scripts/python.exe -m uvicorn app.main:app "
                  f"--host 127.0.0.1 --port 8000", file=sys.stderr)
            return 1
        eng = (h.get("data") or {}).get("ocr_engine") or {}
        print(f"后端 OK · OCR 引擎：{eng.get('name')} · optimized={eng.get('optimized')}")
        if eng.get("optimized") is False:
            print("⚠️  当前是 Mock 引擎，识别结果是占位文本，本次入库的语料不可用于评估！",
                  file=sys.stderr)

        known = existing_names(client) if args.skip_existing else set()
        if args.skip_existing:
            print(f"已入库文件名 {len(known)} 个，将跳过同名文件")

        print("-" * 66)
        ok = skip = fail = 0
        for p in pdfs:
            if p.name in known:
                print(f"  [跳过] {p.name}")
                skip += 1
                continue

            t0 = time.time()
            try:
                with p.open("rb") as fh:
                    r = client.post(
                        BASE + "/api/file/upload",
                        files={"file": (p.name, fh, "application/pdf")},
                        timeout=UPLOAD_TIMEOUT,
                    )
                up = r.json()
                if up.get("code") != 200:
                    print(f"  [失败] {p.name} 上传：{up.get('msg')}")
                    fail += 1
                    continue
                doc_id = up["data"]["doc_id"]

                r = client.post(BASE + "/api/ocr/recognize",
                                json={"doc_id": doc_id}, timeout=OCR_TIMEOUT)
                oc = r.json()
                if oc.get("code") != 200:
                    print(f"  [失败] {p.name} 识别：{oc.get('msg')}")
                    fail += 1
                    continue

                dd = oc.get("data") or {}
                text_len = len(dd.get("ocr_raw_text") or "")
                cost = dd.get("infer_cost") or 0
                print(f"  [成功] {p.name}  doc_id={doc_id:>3}  "
                      f"{cost:>8.0f}ms  文本 {text_len:>5} 字  "
                      f"字段 {len(dd.get('extracts') or [])} 个  "
                      f"（{time.time()-t0:.1f}s）")

                mapping[p.name] = {
                    "doc_id": doc_id,
                    "text_len": text_len,
                    "infer_cost_ms": cost,
                }
                ok += 1
            except Exception as e:
                print(f"  [失败] {p.name} 异常：{type(e).__name__}: {e}")
                fail += 1

            mapping_path.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        print("-" * 66)
        print(f"入库结果：成功 {ok} · 跳过 {skip} · 失败 {fail}")

        if not args.no_index and ok > 0:
            print("\n重建 RAG 索引……")
            try:
                r = client.post(BASE + "/api/rag/index", json={"doc_ids": None},
                                timeout=600.0)
                print(f"  索引响应：{r.json().get('msg')}")
                st = _get(client, "/api/rag/index/status")
                print(f"  索引状态：{json.dumps(st.get('data'), ensure_ascii=False)}")
            except Exception as e:
                print(f"  索引重建失败：{type(e).__name__}: {e}", file=sys.stderr)

    print(f"\n映射文件：{mapping_path}")
    print("下一步：扩充 scripts/eval_set.json 后重跑 scripts/eval_rag.py")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
