# -*- coding: utf-8 -*-
"""
版本快照服务（方案 B）

有效版本的**唯一发布入口**与读取入口。核心规则（任务书 3.1/3.2）：

- OCR 成功 / 人工编辑 / 切换类型都通过 publish_revision() 产生新版本；
  本函数不写死 commit —— 调用方把它包在自己的事务里统一提交，保证
  「快照 + 当前字段 + document_info 指针」原子生效，不存在半套版本。
- 无实际变化不创建空版本（调用方负责比对，见 routers/document.py 的 PATCH）。
- reviewed_revision 语义：等于 current_revision 即"已校对"；
  任何内容/类型变更都会把它清掉（mark_reviewed=True 的显式操作除外）。
- OCR 原始文本只记录识别当时的结果，人工编辑永远不改它。
"""
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import DocumentExtract, DocumentInfo, DocumentRevision

logger = logging.getLogger(__name__)

# 变更原因枚举（与 models.DocumentRevision.change_reason 注释一致）
REASON_OCR = "ocr"
REASON_EDIT = "edit"
REASON_RETYPE = "retype"
REASON_LEGACY = "legacy"


def publish_revision(
    db: Session,
    doc: DocumentInfo,
    *,
    change_reason: str,
    doc_type: str,
    ocr_raw_text: Optional[str],
    effective_text: Optional[str],
    fields: List[Dict[str, Any]],
    engine_type: Optional[str],
    mark_reviewed: bool = False,
) -> int:
    """
    在调用方事务内发布新有效版本，返回新版本号。

    fields 元素约定：{field_key, field_name, field_value, field_value_raw}
    （field_value 为规范值：日期 ISO / 金额十进制字符串 / 文本原文；空值为 None）
    """
    next_rev = (doc.current_revision or 0) + 1
    # Compare-and-swap inside the write transaction: two requests that read r1
    # may not both publish r2, and an OCR result may not resurrect a deletion.
    matched = db.query(DocumentInfo).filter(
        DocumentInfo.id == doc.id, DocumentInfo.deleted_at.is_(None),
        DocumentInfo.current_revision == doc.current_revision,
    ).update({DocumentInfo.current_revision: next_rev}, synchronize_session=False)
    if not matched:
        db.rollback()
        from fastapi import HTTPException
        raise HTTPException(409, '文档已被修改或删除，请重新载入后再试')

    snapshot = [{
        "field_key": f.get("field_key"),
        "field_name": f.get("field_name"),
        "field_value": f.get("field_value"),
        "field_value_raw": f.get("field_value_raw"),
    } for f in fields]

    db.add(DocumentRevision(
        doc_id=doc.id,
        revision=next_rev,
        change_reason=change_reason,
        doc_type=doc_type,
        ocr_raw_text=ocr_raw_text,
        effective_text=effective_text,
        fields_json=json.dumps(snapshot, ensure_ascii=False),
        engine_type=engine_type,
    ))

    # 同步当前字段表：整组替换（当前字段只有一份，历史在各版本快照里）
    db.query(DocumentExtract).filter(
        DocumentExtract.doc_id == doc.id
    ).delete(synchronize_session=False)
    for f in snapshot:
        db.add(DocumentExtract(
            doc_id=doc.id,
            field_name=f["field_name"] or "",
            field_value=f["field_value"],
            field_key=f["field_key"],
            field_value_raw=f["field_value_raw"],
            revision=next_rev,
        ))

    doc.current_revision = next_rev
    doc.doc_type = doc_type
    doc.ocr_raw_text = ocr_raw_text
    doc.engine_type = engine_type
    doc.reviewed_revision = next_rev if mark_reviewed else None
    logger.info(f"文档 {doc.id} 发布版本 r{next_rev}（{change_reason}，{doc_type}）")
    return next_rev


def get_effective_snapshot(
    db: Session, doc_id: int, revision: Optional[int] = None
) -> Optional[DocumentRevision]:
    """读取指定版本快照；不传 revision 时取文档当前有效版本。"""
    q = db.query(DocumentRevision).filter(DocumentRevision.doc_id == doc_id)
    if revision is not None:
        return q.filter(DocumentRevision.revision == revision).first()
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc or doc.current_revision is None:
        return None
    return q.filter(DocumentRevision.revision == doc.current_revision).first()


def list_revisions(db: Session, doc_id: int) -> List[DocumentRevision]:
    """文档的全部版本快照（按版本号升序）"""
    return (
        db.query(DocumentRevision)
        .filter(DocumentRevision.doc_id == doc_id)
        .order_by(DocumentRevision.revision.asc())
        .all()
    )


def snapshot_fields(rev: DocumentRevision) -> List[Dict[str, Any]]:
    """解析快照里的字段 JSON（容错：坏 JSON 返回空列表而不是炸接口）"""
    try:
        return json.loads(rev.fields_json) if rev.fields_json else []
    except (json.JSONDecodeError, TypeError):
        logger.warning(f"文档 {rev.doc_id} r{rev.revision} 字段快照 JSON 损坏")
        return []


def is_reviewed(doc: DocumentInfo) -> bool:
    """当前版本是否已校对"""
    return (
        doc.current_revision is not None
        and doc.reviewed_revision == doc.current_revision
    )
