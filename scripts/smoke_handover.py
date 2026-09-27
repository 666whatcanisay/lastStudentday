"""Read-only local production smoke. No uploads, cloud calls, or valid edits."""
import argparse
import io
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import fitz
import httpx
import openpyxl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/handover_smoke"))
    args = parser.parse_args()
    if urlsplit(args.base_url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Local service required")
    password = os.environ.get("DOC_OCR_SMOKE_GUEST_PASSWORD")
    if not password:
        parser.error("Set DOC_OCR_SMOKE_GUEST_PASSWORD; never commit it")
    results = {}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=args.base_url, timeout=90, trust_env=False) as client:
        for path in ("/api/document/list", "/api/file/preview/6", "/api/document/export/report?doc_id=6"):
            assert client.get(path).status_code == 401, path
        results["unauthenticated_reads"] = "denied"
        assert client.get("/api/health", headers={"Host": "evil.invalid"}).status_code == 403
        results["wrong_host"] = "denied"
        login = client.post("/api/auth/login", json={"password": password},
                            headers={"Origin": args.base_url})
        login.raise_for_status()
        assert login.json()["data"]["role"] == "guest"
        token = login.cookies.get("doc_ocr_demo_session")
        assert token
        # Secure cookies are not automatically sent by httpx over loopback HTTP.
        client.headers["Cookie"] = f"doc_ocr_demo_session={token}"
        client.headers["Origin"] = args.base_url
        results["guest_login"] = "passed"
        for path in ("/api/maintenance/backup", "/api/rag/index"):
            assert client.post(path, json={}).status_code == 403, path
        for path in ("/api/document/list?in_recycle_bin=true",):
            assert client.get(path).status_code == 403, path
        assert client.delete("/api/document/6").status_code == 403
        results["guest_admin_operations"] = "denied"
        rejected = client.patch("/api/document/6", json={"expected_revision": -1},
                                headers={"Origin": "https://evil.invalid"})
        assert rejected.status_code == 403
        conflict = client.patch("/api/document/6", json={"expected_revision": -1})
        assert conflict.json()["code"] == 409
        results["csrf_and_revision_conflict"] = "passed_no_write"
        summary = client.get("/api/document/analytics").json()["data"]["summary"]
        assert summary["doc_count"] == summary["reviewed_count"] == 26
        assert summary["missing_date_count"] == 11
        assert summary["missing_currency_count"] == 10
        results["analytics"] = summary
        results["index_status"] = client.get("/api/rag/index/status").json()["data"]
        search = client.post("/api/rag/search", json={
            "query": "信息技术服务费发票 价税合计", "doc_ids": [6], "mode": "hybrid"})
        data = search.json()["data"]
        assert data["hits"], "Expected local retrieval evidence"
        results["local_retrieval"] = {"hits": len(data["hits"]), "cloud_called": False}
        pdf = client.get("/api/document/export/report", params={"doc_id": 6})
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        with fitz.open(stream=pdf.content, filetype="pdf") as rendered:
            assert len(rendered) >= 1
            results["pdf_pages"] = len(rendered)
            for number, page in enumerate(rendered, start=1):
                page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2)).save(
                    args.output_dir / f"report-page-{number}.png")
        (args.output_dir / "sample-report.pdf").write_bytes(pdf.content)
        excel = client.get("/api/document/export", params={"doc_ids": "6,13"})
        workbook = openpyxl.load_workbook(io.BytesIO(excel.content))
        results["excel_sheets"] = workbook.sheetnames
        workbook.close()
        (args.output_dir / "sample-export.xlsx").write_bytes(excel.content)
        client.post("/api/auth/logout")
    (args.output_dir / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("交付冒烟通过：访客登录、权限、冲突、统计、本地检索、PDF/Excel；未调用云端或修改文档。")


if __name__ == "__main__":
    main()
