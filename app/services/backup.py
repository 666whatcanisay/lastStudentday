# -*- coding: utf-8 -*-
"""
一致性备份服务（方案 B 3.6）

产物 ZIP 结构：
  backup_meta.json   格式版本/schema 版本/应用版本/创建时间/统计
  manifest.json      每个成员：相对路径、大小、SHA-256
  data/doc_ocr_v2.db 一致性库副本（SQLite Online Backup API，WAL 下安全）
  uploads/<相对路径>  全部被数据库引用的原件

规则：
- 原件缺失 → 整体失败，不产出"缺件但自称完整"的备份
- 输出在 BACKUP_DIR（data/backups/），不在 uploads 内，避免递归备份自身
- 不含 .env/Key、日志、模型、虚拟环境、历史备份
- 备份未加密且含敏感文档，界面须提示用户另存
"""
import hashlib
import json
import logging
import sqlite3
import threading
import zipfile
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import (
    APP_VERSION, BACKUP_DIR, DATA_ROOT, DATABASE_PATH, SCHEMA_VERSION,
    TMP_DIR, UPLOAD_DIR,
)
from app.models import DocumentInfo
from app.services.backup_gate import GATE, BackupTimeout

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = 1

# 同时只允许一个备份任务（前端不得反复创建；后端硬保证）
_backup_running = threading.Lock()


class BackupError(RuntimeError):
    """备份失败（原件缺失/磁盘错误等）"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup_zip(db: Session) -> Path:
    """
    在写入门禁窗口内生成一致性备份 ZIP，返回文件路径。
    可能抛出：BackupTimeout（忙，60s 超时）、BackupError（原件缺失等）。
    """
    if not _backup_running.acquire(blocking=False):
        raise BackupError("已有备份任务正在进行，请勿重复发起")
    try:
        with GATE.backup_window(timeout=60.0):
            return _do_backup(db)
    finally:
        _backup_running.release()


def _do_backup(db: Session) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S") + '_' + uuid.uuid4().hex[:8]
    staging_db = TMP_DIR / f"backup_staging_{ts}.db"
    zip_path = BACKUP_DIR / f"doc_ocr_backup_{ts}.zip"

    try:
        # 1. 库一致性副本（门禁窗口内，无并发写入，backup API 保证页级一致）
        src = sqlite3.connect(str(DATABASE_PATH))
        try:
            dst = sqlite3.connect(str(staging_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # 2. 收集被引用原件（全部文档含回收站；缺失即整体失败）
        docs = db.query(DocumentInfo).all()
        missing = []
        members = []  # (zip_arcname, abs_path)
        for d in docs:
            fp = Path(d.file_path).resolve()
            if not fp.exists():
                missing.append({"doc_id": d.id, "file_name": d.file_name})
                continue
            try:
                rel = fp.relative_to(Path(UPLOAD_DIR).resolve())
            except ValueError:
                raise BackupError(f"文档 #{d.id} 的原件不在受控存储目录，已拒绝备份")
            members.append((f"uploads/{rel.as_posix()}", fp))
        if missing:
            raise BackupError(
                f"有 {len(missing)} 份原件缺失，无法生成完整备份："
                + ", ".join(f"#{m['doc_id']} {m['file_name']}" for m in missing[:5]))

        # 3. 写 ZIP（成员去重：多文档理论上不共享原件，防御性处理）
        seen = set()
        manifest = {"files": []}
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            meta = {
                "format_version": BACKUP_FORMAT_VERSION,
                "schema_version": SCHEMA_VERSION,
                "app_version": APP_VERSION,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "doc_count": len(docs),
            }
            zf.writestr("backup_meta.json",
                        json.dumps(meta, ensure_ascii=False, indent=2))

            for arc, fp in members:
                if arc in seen:
                    continue
                seen.add(arc)
                digest = _sha256_file(fp)
                zf.write(fp, arc)
                manifest["files"].append(
                    {"path": arc, "size": fp.stat().st_size, "sha256": digest})

            db_digest = _sha256_file(staging_db)
            zf.write(staging_db, "data/doc_ocr_v2.db")
            manifest["files"].append({
                "path": "data/doc_ocr_v2.db",
                "size": staging_db.stat().st_size,
                "sha256": db_digest,
            })
            zf.writestr("manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2))

        logger.info(f"备份完成：{zip_path}（{len(docs)} 文档，{len(manifest['files'])} 成员）")
        return zip_path
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise
    finally:
        staging_db.unlink(missing_ok=True)
