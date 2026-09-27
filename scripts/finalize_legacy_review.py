"""Apply an explicitly approved conservative worksheet through existing APIs.

Dry run by default. Refuse changed revisions before backing up or editing.
No direct database UPDATE, online restore, or model calls are performed.
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services.field_registry import specs_for, normalize_value
from scripts.restore_backup import precheck


def build_payload(row):
    kind = row["suggested_type"]
    specs = {spec.key: spec for spec in specs_for(kind)}
    fields = {key: None for key in specs}
    for key, value in json.loads(row["source_field_candidates"]).items():
        if key not in specs:
            raise ValueError(f"Unexpected field: {key}")
        fields[key] = normalize_value(specs[key], value, strict=True)
    # Generic 日期 never becomes invoice_date/signed_date. No currency guessing.
    fields["currency"] = None
    if kind == "other":
        amount = row.get("source_summary_amount")
        fields["notes"] = (
            f"原件金额：{amount}；币种未确认。" if amount else ""
        ) + "已按原件核对，未知字段保持空值。"
    return {"expected_revision": int(row["revision"]), "doc_type": kind,
            "fields": fields, "mark_reviewed": True}


def checked_json(response):
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 200:
        raise RuntimeError(result.get("msg") or "API rejected the request")
    return result["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--confirm-reviewed", action="store_true")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Only a local maintenance server is allowed")
    with args.worksheet.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if ({int(row["doc_id"]) for row in rows} != set(range(2, 28))
            or len(rows) != 26
            or Counter(row["suggested_type"] for row in rows)
            != Counter(invoice=5, contract=5, other=16)):
        parser.error("Worksheet differs from the user-approved 26-document batch")
    report = {"started_at": datetime.now().isoformat(), "status": "preflight", "applied": []}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with httpx.Client(base_url=args.base_url, timeout=120, trust_env=False) as client:
            all_docs = checked_json(client.get("/api/document/list", params={"page_size": 100}))["items"]
            valid = {doc["id"] for doc in all_docs if doc["current_revision"] is not None}
            if valid != {int(row["doc_id"]) for row in rows}:
                raise RuntimeError("Active document set changed; re-audit before applying")
            for row in rows:
                current = checked_json(client.get(f'/api/document/{row["doc_id"]}'))
                if (current["current_revision"] != int(row["revision"])
                        or current["file_name"] != row["file_name"]
                        or current["doc_type"] != row["current_type"]):
                    raise RuntimeError(f'Document {row["doc_id"]} changed; nothing applied')
                build_payload(row)
            if not args.confirm_reviewed:
                report["status"] = "dry_run_passed"
                print("预检通过：26 份；未备份、未修改。正式操作须 --confirm-reviewed。")
                return
            args.backup_dir.mkdir(parents=True, exist_ok=True)
            backup = args.backup_dir / f'pre_workbench_{datetime.now():%Y%m%d_%H%M%S}.zip'
            response = client.post("/api/maintenance/backup")
            response.raise_for_status()
            if "zip" not in response.headers.get("content-type", ""):
                raise RuntimeError(f"Backup rejected: {response.text[:300]}")
            with backup.open("xb") as handle:
                handle.write(response.content)
            info = precheck(backup)
            report["backup"] = {"path": str(backup.resolve()), "validated": True,
                                "sha256": hashlib.sha256(response.content).hexdigest(),
                                "schema_version": info["meta"]["schema_version"]}
            for row in rows:
                payload = build_payload(row)
                saved = checked_json(client.patch(f'/api/document/{row["doc_id"]}', json=payload))
                values = {f["field_key"]: f["field_value"] for f in saved["extracts"]}
                if (saved["doc_type"] != row["suggested_type"] or not saved["reviewed"]
                        or any(values.get(key) != value for key, value in payload["fields"].items())
                        or any(not key for key in values)):
                    raise RuntimeError(f'Document {row["doc_id"]} post-save verification failed')
                report["applied"].append({"doc_id": saved["id"], "type": saved["doc_type"],
                                           "revision": saved["current_revision"], "reviewed": True})
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            report["analytics"] = checked_json(client.get("/api/document/analytics"))["summary"]
            report["status"] = "complete"
            print(f"完成：{len(report['applied'])} 份经 API 保存并校对；备份 {backup}")
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
