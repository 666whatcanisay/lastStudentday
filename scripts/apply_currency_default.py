"""Backfill missing invoice/contract currency through the versioned document API.

Dry run by default. With --apply, first download and validate a complete backup.
Never overwrite a recorded currency or a document that explicitly names another one.
"""

import argparse
import getpass
import hashlib
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import BACKUP_DIR
from app.services.field_registry import explicit_currency_codes
from scripts.restore_backup import precheck


def checked(response):
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 200:
        raise RuntimeError(body.get("msg") or "API 拒绝了操作")
    return body["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--apply", action="store_true", help="备份后逐份发布新版本")
    parser.add_argument("--backup-zip", type=Path,
                        help="使用本次操作前已生成并校验的完整备份，避免重复下载")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None or parsed.path not in ("", "/")):
        parser.error("只允许指定本机 HTTP 服务地址")

    password = getpass.getpass("当前管理员口令（不会写入文件）：")
    with httpx.Client(base_url=args.base_url, timeout=120, trust_env=False,
                      headers={"Origin": args.base_url.rstrip("/")}) as client:
        login = checked(client.post("/api/auth/login", json={"password": password}))
        if login.get("role") != "admin":
            raise RuntimeError("需要管理员权限")
        del password

        targets = []
        skipped_explicit = []
        page = 1
        while True:
            batch = checked(client.get("/api/document/list",
                                       params={"page": page, "page_size": 100}))
            for item in batch["items"]:
                if item.get("doc_type") not in {"invoice", "contract"} or item.get("current_revision") is None:
                    continue
                doc = checked(client.get(f'/api/document/{item["id"]}'))
                fields = {f.get("field_key"): f.get("field_value") for f in doc["extracts"]}
                if fields.get("currency"):
                    continue
                explicit = explicit_currency_codes(doc.get("effective_text") or "")
                if explicit:
                    skipped_explicit.append((doc["id"], ",".join(sorted(explicit))))
                else:
                    targets.append((doc["id"], doc["current_revision"], bool(doc["reviewed"])))
            if page * 100 >= batch["total"]:
                break
            page += 1

        print(f"预检：待默认人民币 {len(targets)} 份，明确标有币种需人工核对 {len(skipped_explicit)} 份。")
        summary = checked(client.get("/api/document/analytics"))["summary"]
        print(f"当前统计：有效文档 {summary['doc_count']}；币种缺失 {summary['missing_currency_count']}；"
              f"未知币种金额 {summary['unknown_currency_amount_count']}")
        if skipped_explicit:
            print("暂不自动处理的文档 ID：" + ", ".join(str(i) for i, _ in skipped_explicit))
        if not args.apply or not targets:
            print("未修改数据。" if not args.apply else "无待处理文档。")
            return

        if args.backup_zip:
            backup = args.backup_zip.resolve()
            if not backup.is_relative_to(BACKUP_DIR.resolve()):
                raise RuntimeError("现有备份必须位于本项目的备份目录")
        else:
            response = client.post("/api/maintenance/backup")
            response.raise_for_status()
            if "zip" not in response.headers.get("content-type", ""):
                raise RuntimeError("备份接口未返回 ZIP，停止写入")
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / f'pre_default_cny_{datetime.now():%Y%m%d_%H%M%S}.zip'
            with backup.open("xb") as stream:
                stream.write(response.content)
        info = precheck(backup)
        if info["meta"]["doc_count"] < len(targets):
            raise RuntimeError("备份文档数少于待修改数，停止写入")
        with zipfile.ZipFile(backup) as archive, tempfile.TemporaryDirectory() as temp_dir:
            snapshot_path = Path(temp_dir) / "snapshot.db"
            snapshot_path.write_bytes(archive.read("data/doc_ocr_v2.db"))
            with sqlite3.connect(str(snapshot_path)) as snapshot:
                ids = [doc_id for doc_id, _, _ in targets]
                marks = ",".join("?" for _ in ids)
                saved_revisions = dict(snapshot.execute(
                    f"SELECT id, current_revision FROM document_info WHERE id IN ({marks})", ids))
            if any(saved_revisions.get(doc_id) != revision for doc_id, revision, _ in targets):
                raise RuntimeError("备份与当前待修改版本不一致，停止写入")
        with backup.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        print(f"完整备份已校验：{backup}；SHA-256 {digest}")

        applied = []
        for doc_id, revision, reviewed in targets:
            saved = checked(client.patch(f"/api/document/{doc_id}", json={
                "expected_revision": revision,
                "fields": {"currency": "CNY"},
                "mark_reviewed": reviewed,
            }))
            values = {f.get("field_key"): f.get("field_value") for f in saved["extracts"]}
            if (values.get("currency") != "CNY"
                    or saved["current_revision"] != revision + 1
                    or bool(saved["reviewed"]) != reviewed):
                raise RuntimeError(f"文档 #{doc_id} 保存后验证失败；此前已完成 {applied}")
            applied.append(doc_id)
            print(f"#{doc_id}：r{revision} → r{revision + 1}，币种 CNY")
        print(f"完成 {len(applied)} 份；备份保存在 {backup}")


if __name__ == "__main__":
    main()
