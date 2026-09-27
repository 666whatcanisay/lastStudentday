"""Read-only legacy-document audit against an isolated SQLite copy.

The CSV is intentionally local-only: it contains document names and field values.
It is a review worksheet, not an automatic migration or approval to edit records.
"""

import argparse
import csv
import difflib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.extractor import extract_for_type
from app.services.field_registry import classify_doc_type, infer_currency, normalize_amount, normalize_date


def source_text(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "", "原件缺失"
    if path.suffix.lower() != ".pdf":
        return "", "图片原件需人工目视核对"
    try:
        import fitz

        with fitz.open(path) as pdf:
            text = "\n".join(page.get_text(sort=True) for page in pdf)
        return text, "PDF 文本层" if text.strip() else "PDF 无文本层，需人工目视核对"
    except Exception as exc:
        return "", f"PDF 读取失败：{type(exc).__name__}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="隔离数据库副本")
    parser.add_argument("--output", type=Path, required=True, help="本机私有 CSV")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("隔离数据库副本不存在")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT d.id, d.file_name, d.file_path, d.doc_type, d.current_revision,
                  d.reviewed_revision, r.effective_text, r.fields_json
           FROM document_info d
           JOIN document_revision r
             ON r.doc_id = d.id AND r.revision = d.current_revision
           WHERE d.deleted_at IS NULL
           ORDER BY d.id"""
    ).fetchall()

    columns = [
        "doc_id", "file_name", "revision", "current_type", "reviewed",
        "source_status", "source_vs_ocr", "suggested_type", "type_evidence",
        "source_date_label", "source_date", "source_summary_amount", "source_currency",
        "source_field_candidates", "current_fields", "review_decision",
        "confirmed_business_date", "confirmed_amount", "confirmed_currency",
        "uncertainty_and_evidence", "approved_for_apply",
    ]
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            source, status = source_text(Path(row["file_path"]))
            effective = row["effective_text"] or ""
            # The original source, not the filename, is the basis for suggestions.
            basis = source or effective
            suggested = classify_doc_type(basis)
            candidates = {
                item["field_key"]: item["field_value"]
                for item in extract_for_type(basis, suggested)
                if item["field_value"] is not None
            }
            current = {
                item.get("field_key", ""): item.get("field_value")
                for item in json.loads(row["fields_json"] or "[]")
                if item.get("field_value") is not None
            }
            date_match = re.search(
                r"(开票日期|签订日期|签约日期|日期)[：:\s]*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)",
                basis,
            )
            amount_match = re.search(
                r"(?:价税合计|金额合计|合计金额|合同金额|总金额)[^\n]{0,5}?[：:]\s*[￥¥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
                basis,
            )
            if not amount_match:
                amount_match = re.search(r"[（(]小写[）)]\s*[￥¥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", basis)
            if not amount_match and Path(row["file_path"]).suffix.lower() != ".pdf":
                amount_match = re.search(r"金额[：:]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", basis)
            source_compact = re.sub(r"\s+", "", source)
            effective_compact = re.sub(r"\s+", "", effective)
            similarity = (round(difflib.SequenceMatcher(None, source_compact, effective_compact).ratio(), 3)
                          if source else "需目视核对")
            uncertainty = []
            if not source:
                uncertainty.append("无 PDF 文本层：必须看原件")
            if date_match and date_match.group(1) == "日期" and suggested in {"invoice", "contract"}:
                uncertainty.append("原件只标“日期”，不能自动等同开票/签订日期")
            if not infer_currency(basis):
                uncertainty.append("币种缺少明确证据；“元”/“¥”不自动认定 CNY")
            writer.writerow({
                "doc_id": row["id"],
                "file_name": row["file_name"],
                "revision": row["current_revision"],
                "current_type": row["doc_type"],
                "reviewed": row["reviewed_revision"] == row["current_revision"],
                "source_status": status,
                "source_vs_ocr": similarity,
                "suggested_type": suggested,
                "type_evidence": basis[:160].replace("\n", " "),
                "source_date_label": date_match.group(1) if date_match else "",
                "source_date": normalize_date(date_match.group(2)) if date_match else "",
                "source_summary_amount": normalize_amount(amount_match.group(1)) if amount_match else "",
                "source_currency": infer_currency(basis) or "未知",
                "source_field_candidates": json.dumps(candidates, ensure_ascii=False),
                "current_fields": json.dumps(current, ensure_ascii=False),
                "review_decision": "待用户确认，不自动写回",
                "confirmed_business_date": "",
                "confirmed_amount": "",
                "confirmed_currency": "",
                "uncertainty_and_evidence": "；".join(uncertainty),
                "approved_for_apply": "否",
            })
    db.close()
    print(f"审核表已生成：{args.output}（{len(rows)} 份有效文档；未修改数据库）")


if __name__ == "__main__":
    main()
