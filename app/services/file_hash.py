# -*- coding: utf-8 -*-
"""
文件哈希与内容去重服务（方案 B 3.3）

- 流式 SHA-256：与上传分块写入同步累计，不二次读盘
- 完全相同内容默认不新增、不再次 OCR；不比较文件名，不做视觉/语义去重
- 并发重复上传由进程级互斥锁保证"查询→插入"窗口原子，最终只保留一份
- 旧文件分批补算哈希：缺失原件只报告，不删除记录
"""
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.models import DocumentInfo

logger = logging.getLogger(__name__)

# 「按哈希查重 → 决定 → 插入」窗口的进程级互斥（配合 file_sha256 唯一约束）
_dedup_lock = threading.Lock()


def new_hasher() -> "hashlib._Hash":
    return hashlib.sha256()


def find_by_hash(db: Session, digest: str) -> Optional[DocumentInfo]:
    return db.query(DocumentInfo).filter(
        DocumentInfo.file_sha256 == digest).first()


def dedup_lock() -> threading.Lock:
    return _dedup_lock


def backfill_missing_hashes(db: Session, batch_size: int = 50) -> dict:
    """
    为旧记录补算内容哈希（分批，供启动后台任务循环调用直至 done）。
    返回 {done, scanned, filled, missing_files}；missing_files 只报告不删记录。
    """
    docs = (
        db.query(DocumentInfo)
        .filter(DocumentInfo.file_sha256.is_(None))
        .order_by(DocumentInfo.id.asc())
        .limit(batch_size)
        .all()
    )
    filled = 0
    missing = []
    for doc in docs:
        fp = Path(doc.file_path)
        if not fp.exists():
            missing.append({"doc_id": doc.id, "file_name": doc.file_name})
            # 标记为已扫描但无法计算：写空串占位会破坏唯一约束，留 NULL 并靠
            # 调用方报告；为避免每轮重复扫描同一批，跳过策略见调用方注释
            continue
        h = new_hasher()
        try:
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            # 与既有记录撞哈希：说明历史已有重复 → 不擅自合并（任务书 3.3），
            # 留 NULL 让统计与上传去重都按"未知哈希"处理，由界面提示
            clash = find_by_hash(db, digest)
            if clash and clash.id != doc.id:
                logger.warning(
                    f"补算哈希发现历史重复：doc#{doc.id} 与 doc#{clash.id} 内容相同，"
                    "按规则不自动合并，保留待人工处理")
                continue
            doc.file_sha256 = digest
            filled += 1
        except Exception as e:
            logger.warning(f"补算哈希失败 doc#{doc.id}: {e}")
            missing.append({"doc_id": doc.id, "file_name": doc.file_name})
    db.commit()
    return {
        "done": len(docs) < batch_size,
        "scanned": len(docs),
        "filled": filled,
        "missing_files": missing,
    }
