"""
文档管理路由；统计与导出已分到独立路由模块，但外部 URL 不变。
  GET    /api/document/list                       分页列表（摘要，支持类型/校对/回收站过滤）
  GET    /api/document/{doc_id}                   详情（原始/有效文本、类型字段、版本、预览 URL）
  PATCH  /api/document/{doc_id}                   编辑（乐观并发 409 + 版本发布）
  GET    /api/document/{doc_id}/revisions         版本历史摘要
  GET    /api/document/{doc_id}/revisions/{rev}   只读版本快照
  DELETE /api/document/{doc_id}                   软删除（进回收站）
  POST   /api/document/{doc_id}/restore           从回收站恢复
  DELETE /api/document/{doc_id}/permanent         永久删除（仅回收站条目）

⚠️ main.py 必须先注册统计与导出路由，再注册这里的 /{doc_id} 动态路径。
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.config import UPLOAD_DIR
from app.database import get_db
from app.models import DocumentInfo, DocumentExtract
from app.schemas import (
    ApiResponse, ExtractFieldOut, DocumentListItemOut, RevisionSummaryOut,
    DocumentPatchRequest,
)
from app.services import revisions as rev_svc
from app.services import field_registry as registry
from app.services.extractor import extract_for_type
from app.services.backup_gate import GATE, BackupInProgress

router = APIRouter(prefix="/api/document", tags=["文档管理"])
logger = logging.getLogger(__name__)

# 切换类型时跨类型保留的公共字段键（如币种）：它们与单据类型无关，
# 重新抽取很可能抽不出来（币种是全文推断），丢了就要用户重填，属于无谓损失。
PUBLIC_FIELD_KEYS = {"currency"}


# ==================== 序列化辅助 ====================

def _extract_outs(doc: DocumentInfo) -> List[ExtractFieldOut]:
    return [
        ExtractFieldOut(id=e.id, doc_id=e.doc_id, field_name=e.field_name,
                        field_value=e.field_value, field_key=e.field_key)
        for e in doc.extracts
    ]


def _list_item(doc: DocumentInfo) -> dict:
    item = DocumentListItemOut(
        id=doc.id,
        file_name=doc.file_name,
        file_type=doc.file_type,
        upload_time=doc.upload_time,
        doc_type=doc.doc_type or "other",
        status=doc.status,
        error_msg=doc.error_msg,
        infer_cost=doc.infer_cost,
        current_revision=doc.current_revision,
        reviewed=rev_svc.is_reviewed(doc),
        deleted_at=doc.deleted_at,
        engine_type=doc.engine_type,
        last_attempt_status=doc.last_attempt_status,
        last_attempt_error=doc.last_attempt_error,
        index_status=doc.index_state.status if doc.index_state else None,
        indexed_revision=doc.index_state.indexed_revision if doc.index_state else None,
        extracts=_extract_outs(doc),
    )
    return item.model_dump()


def _detail_dict(doc: DocumentInfo, db: Session) -> dict:
    """详情：原始/有效文本分离，版本与状态齐全，原件只给相对 URL（不给绝对路径）"""
    snap = rev_svc.get_effective_snapshot(db, doc.id)
    return {
        "id": doc.id,
        "file_name": doc.file_name,
        "file_type": doc.file_type,
        "upload_time": doc.upload_time.isoformat() if doc.upload_time else None,
        "doc_type": doc.doc_type or "other",
        "status": doc.status,
        "error_msg": doc.error_msg,
        "infer_cost": doc.infer_cost,
        "ocr_raw_text": doc.ocr_raw_text,
        "effective_text": snap.effective_text if snap else None,
        "current_revision": doc.current_revision,
        "reviewed": rev_svc.is_reviewed(doc),
        "deleted_at": doc.deleted_at.isoformat() if doc.deleted_at else None,
        "engine_type": doc.engine_type,
        "last_attempt_status": doc.last_attempt_status,
        "last_attempt_error": doc.last_attempt_error,
        "index_status": doc.index_state.status if doc.index_state else None,
        "indexed_revision": doc.index_state.indexed_revision if doc.index_state else None,
        "preview_url": f"/api/file/preview/{doc.id}",
        "extracts": [e.model_dump() for e in _extract_outs(doc)],
    }


# ==================== 固定路由（必须先于 /{doc_id}） ====================

@router.get("/list", response_model=ApiResponse)
def get_document_list(
    keyword: Optional[str] = Query(None, description="搜索关键字（文件名或识别文本）"),
    doc_type: Optional[str] = Query(None, description="类型过滤：invoice/contract/other"),
    reviewed: Optional[bool] = Query(None, description="校对状态过滤"),
    in_recycle_bin: bool = Query(False, description="true=看回收站；false=正常列表"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数"),
    db: Session = Depends(get_db),
):
    """
    分页文档列表（摘要视图：不含全文与绝对路径）。
    回收站与正常列表互斥；日期/币种等统计过滤见 /analytics（P3）。
    """
    query = db.query(DocumentInfo)

    if in_recycle_bin:
        query = query.filter(DocumentInfo.deleted_at.isnot(None))
    else:
        query = query.filter(DocumentInfo.deleted_at.is_(None))

    if doc_type:
        query = query.filter(DocumentInfo.doc_type == doc_type)

    if reviewed is True:
        query = query.filter(
            DocumentInfo.current_revision.isnot(None),
            DocumentInfo.reviewed_revision == DocumentInfo.current_revision,
        )
    elif reviewed is False:
        query = query.filter(
            or_(
                DocumentInfo.current_revision.is_(None),
                DocumentInfo.reviewed_revision.is_(None),
                DocumentInfo.reviewed_revision != DocumentInfo.current_revision,
            )
        )

    if keyword:
        query = query.filter(
            or_(
                DocumentInfo.file_name.contains(keyword),
                DocumentInfo.ocr_raw_text.contains(keyword),
            )
        )

    total = query.count()
    docs = (
        query.order_by(DocumentInfo.upload_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return ApiResponse.success(data={
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_list_item(d) for d in docs],
    })


# ==================== 编辑辅助 ====================

def _current_fields(db: Session, doc: DocumentInfo) -> tuple:
    """返回 (当前快照, 当前有效字段列表)"""
    snap = rev_svc.get_effective_snapshot(db, doc.id)
    fields = rev_svc.snapshot_fields(snap) if snap else []
    return snap, fields


def _field_map(fields: List[Dict]) -> Dict[str, Optional[str]]:
    """字段列表压成 键→规范值 映射"""
    return {f.get("field_key"): f.get("field_value") for f in fields if f.get("field_key")}


def _field_change_map(fields: List[Dict]) -> Dict[str, str]:
    """
    变更比对专用：**空值键不参与比对**。

    原因：当前类型的注册表可能比这个版本快照建立时多出字段（代码升级/类型扩展），
    编辑时会顺手补齐这些空槽。它们没有业务含义，若参与比对，
    用户点开编辑再直接保存就会莫名多出一个版本。
    而"把有值字段清空"是真实改动 —— 此时旧 map 有值、新 map 无此键，照样能比出来。
    """
    return {k: v for k, v in _field_map(fields).items() if v not in (None, "")}


def _merge_public_fields(extracted: List[Dict], keep: Dict[str, Dict]) -> List[Dict]:
    """切换类型后把公共字段（币种等）的旧值补回去 —— 只在抽取为空时兜底"""
    merged = []
    for row in extracted:
        old = keep.get(row.get("field_key"))
        if old and row.get("field_value") in (None, ""):
            merged.append({
                "field_key": row.get("field_key"),
                "field_name": row.get("field_name"),
                "field_value": old.get("field_value"),
                "field_value_raw": old.get("field_value_raw"),
            })
        else:
            merged.append(dict(row))
    return merged


@router.get("/field-specs", response_model=ApiResponse)
def get_field_specs(doc_type: str = Query("other", description="文档类型：invoice/contract/other")):
    """
    某类型的字段规格（稳定键 + 中文标签 + 值类型）。

    用途：界面切换类型时要先把目标类型的字段列出来给用户看并允许编辑，
    **保存前**不提交任何东西。字段的真相在后端注册表里，前端不硬编码中文名。
    """
    if doc_type not in registry.DOC_TYPES:
        return ApiResponse.error(
            msg=f"不支持的文档类型: {doc_type}（可选 {', '.join(registry.DOC_TYPES)}）",
            code=400)
    return ApiResponse.success(data={
        "doc_type": doc_type,
        "items": [{"key": s.key, "label": s.label, "kind": s.kind}
                  for s in registry.specs_for(doc_type)],
    })


# ==================== 动态路由 ====================

@router.get("/{doc_id}", response_model=ApiResponse)
def get_document_detail(doc_id: int, request: Request, db: Session = Depends(get_db)):
    """文档详情：原始 OCR 文本 + 当前有效文本 + 字段 + 版本/索引状态 + 原件 URL"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc or (doc.deleted_at is not None and
                   getattr(request.state, "demo_role", "admin") == "guest"):
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    return ApiResponse.success(data=_detail_dict(doc, db))


@router.patch("/{doc_id}", response_model=ApiResponse)
def patch_document(doc_id: int, body: DocumentPatchRequest,
                   background: BackgroundTasks,
                   db: Session = Depends(get_db)):
    """
    编辑文档（乐观并发）。

    规则（任务书 3.2 / 3.3）：
      - `expected_revision` 与当前版本不符 → 409，不覆盖他人改动，前端提示重新载入
      - **OCR 原始文本永不被人工编辑覆盖**，编辑只产生新的有效全文
      - 改文本不重抽字段，改字段不改写全文 —— 互不越界
      - 切换类型：目标类型字段从当前有效全文抽取，公共字段（币种）保留旧值，
        原类型字段只留在历史里
      - 有变更才发布新版本；无变更不创建空版本（可单独打"已校对"标记）
      - 发布成功后台触发索引重建（新版本在索引就绪前不参与问答）
    """
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    if doc.deleted_at is not None:
        return ApiResponse.error(msg="文档在回收站中，请先恢复再编辑", code=400)
    if doc.current_revision is None:
        return ApiResponse.error(msg="该文档尚无有效版本，无法编辑", code=400)

    if body.expected_revision != doc.current_revision:
        return ApiResponse.error(
            msg=f"文档已被其他人修改（当前版本 r{doc.current_revision}，"
                f"你的编辑基于 r{body.expected_revision}）。请刷新后重新编辑，本次修改未保存。",
            code=409,
        )

    snap, cur_fields = _current_fields(db, doc)
    cur_text = (snap.effective_text if snap else None) or ""

    target_type = body.doc_type or doc.doc_type or "other"
    if target_type not in registry.DOC_TYPES:
        return ApiResponse.error(
            msg=f"不支持的文档类型: {target_type}（可选 {', '.join(registry.DOC_TYPES)}）",
            code=400)

    type_changed = target_type != (doc.doc_type or "other")
    allowed_keys = set(registry.spec_map(target_type)) | PUBLIC_FIELD_KEYS
    legacy_fields_removed = any(f.get("field_key") not in allowed_keys for f in cur_fields)
    new_text = body.effective_text if body.effective_text is not None else cur_text

    # ---------- 目标版本的字段集合 ----------
    if type_changed:
        # 类型变了才有理由重新抽取（这是用户显式确认过的操作，不是"悄悄"发生）
        keep = {f["field_key"]: f for f in cur_fields
                if f.get("field_key") in PUBLIC_FIELD_KEYS}
        fields = _merge_public_fields(extract_for_type(new_text, target_type), keep)
    else:
        # Retired legacy rows without stable keys stay in historical snapshots,
        # but must not leak into a newly reviewed effective version.
        fields = [dict(f) for f in cur_fields if f.get("field_key") in allowed_keys]
        # 注册表升级后旧快照可能缺字段：补齐为空值，不回放旧版本的抽取结果
        specmap = registry.spec_map(target_type)
        have = {f.get("field_key") for f in fields}
        for key, spec in specmap.items():
            if key not in have:
                fields.append({"field_key": key, "field_name": spec.label,
                               "field_value": None, "field_value_raw": None})

    # ---------- 用户显式传入的字段（严格校验，超限报错而非静默舍入） ----------
    if body.fields is not None:
        specmap = registry.spec_map(target_type)
        overrides: Dict[str, Dict] = {}
        for key, raw in body.fields.items():
            if key not in specmap and key not in PUBLIC_FIELD_KEYS:
                return ApiResponse.error(
                    msg=f"未知字段键：{key}（当前类型 {target_type} 不支持）", code=400)
            spec = specmap.get(key)
            exist = next((f for f in fields if f.get("field_key") == key), None)
            label = (exist or {}).get("field_name") if exist else (spec.label if spec else key)
            try:
                value = registry.normalize_value(spec, raw, strict=True) if spec else (raw or None)
            except ValueError as e:
                return ApiResponse.error(msg=f"字段「{label}」取值不合法：{e}", code=400)
            overrides[key] = {"field_key": key, "field_name": label,
                              # 人工填写没有"抽取原文"这一说，field_value_raw 置空
                              "field_value": value, "field_value_raw": None}

        new_rows: List[Dict[str, Any]] = []
        seen = set()
        for f in fields:
            key = f.get("field_key")
            if key in overrides:
                new_rows.append(overrides[key])
                seen.add(key)
            else:
                new_rows.append(f)
        for key, row in overrides.items():
            if key not in seen:
                new_rows.append(row)
        fields = new_rows

    # ---------- 是否真的有变化 ----------
    text_changed = (new_text or "") != (cur_text or "")
    fields_changed = _field_change_map(fields) != _field_change_map(cur_fields)
    changed = text_changed or fields_changed or type_changed or legacy_fields_removed

    if not changed:
        data = {**_detail_dict(doc, db), "revision_created": False}
        if body.mark_reviewed and not rev_svc.is_reviewed(doc):
            try:
                with GATE.write_section("标记已校对"):
                    doc.reviewed_revision = doc.current_revision
                    db.commit()
            except BackupInProgress as e:
                return ApiResponse.error(msg=str(e), code=409)
            db.expire_all()
            return ApiResponse.success(
                data={**_detail_dict(doc, db), "revision_created": False},
                msg="内容无变化，已标记当前版本为已校对")
        return ApiResponse.success(data=data, msg="无实际变化，未创建新版本")

    reason = rev_svc.REASON_RETYPE if type_changed else rev_svc.REASON_EDIT
    try:
        with GATE.write_section("编辑发布新版本"):
            new_rev = rev_svc.publish_revision(
                db, doc,
                change_reason=reason,
                doc_type=target_type,
                ocr_raw_text=doc.ocr_raw_text,   # 原始 OCR 保持不变
                effective_text=new_text,
                fields=fields,
                engine_type=doc.engine_type,
                mark_reviewed=body.mark_reviewed,
            )
            db.commit()
    except BackupInProgress as e:
        return ApiResponse.error(msg=str(e), code=409)
    except Exception as e:
        db.rollback()
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            raise
        logger.error(f"文档 {doc_id} 发布新版本失败: {e}", exc_info=True)
        return ApiResponse.error(msg="保存失败，请重新载入后重试", code=500)

    db.expire_all()

    # 新版本索引就绪前不参与问答（由 get_eligible_doc_ids 自动排除），这里后台重建
    from app.services.rag_indexer import index_document_background
    background.add_task(index_document_background, doc.id)

    logger.info(f"文档 {doc_id} 编辑已发布 r{new_rev}（{reason}，{target_type}）")
    return ApiResponse.success(
        data={**_detail_dict(doc, db), "revision_created": True, "new_revision": new_rev},
        msg=f"已发布新版本 r{new_rev}")


@router.get("/{doc_id}/revisions", response_model=ApiResponse)
def get_document_revisions(doc_id: int, request: Request, db: Session = Depends(get_db)):
    """版本历史摘要（只读列表；快照详情走 /revisions/{revision}）"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc or (doc.deleted_at is not None and
                   getattr(request.state, "demo_role", "admin") == "guest"):
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    revs = rev_svc.list_revisions(db, doc_id)
    items = [
        RevisionSummaryOut(
            revision=r.revision,
            change_reason=r.change_reason,
            doc_type=r.doc_type,
            engine_type=r.engine_type,
            created_at=r.created_at,
            is_current=(r.revision == doc.current_revision),
            is_reviewed=(r.revision == doc.reviewed_revision),
        ).model_dump()
        for r in revs
    ]
    return ApiResponse.success(data={"items": items, "total": len(items)})


@router.get("/{doc_id}/revisions/{revision}", response_model=ApiResponse)
def get_document_revision_snapshot(doc_id: int, revision: int,
                                   request: Request,
                                   db: Session = Depends(get_db)):
    """只读版本快照：完整文本与字段；回收站外不可见（文档被永久删除后快照随之销毁）"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc or (doc.deleted_at is not None and
                   getattr(request.state, "demo_role", "admin") == "guest"):
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    rev = rev_svc.get_effective_snapshot(db, doc_id, revision)
    if not rev:
        return ApiResponse.error(msg=f"版本不存在: r{revision}", code=404)
    return ApiResponse.success(data={
        "doc_id": doc_id,
        "revision": rev.revision,
        "change_reason": rev.change_reason,
        "doc_type": rev.doc_type,
        "engine_type": rev.engine_type,
        "ocr_raw_text": rev.ocr_raw_text,
        "effective_text": rev.effective_text,
        "fields": rev_svc.snapshot_fields(rev),
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
        "is_current": rev.revision == doc.current_revision,
    })


@router.delete("/{doc_id}", response_model=ApiResponse)
def delete_document(doc_id: int, db: Session = Depends(get_db)):
    """软删除：进回收站。立刻排除列表/统计/导出/问答；原件与历史全部保留。"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    if doc.deleted_at is not None:
        return ApiResponse.error(msg="文档已在回收站", code=400)

    try:
        with GATE.write_section("软删除"):
            doc.deleted_at = datetime.now()
            db.commit()
    except BackupInProgress as e:
        return ApiResponse.error(msg=str(e), code=409)
    logger.info(f"文档 {doc_id} 已软删除（回收站）")
    return ApiResponse.success(msg=f"文档已移入回收站: ID={doc_id}")


@router.post("/{doc_id}/restore", response_model=ApiResponse)
def restore_document(doc_id: int, db: Session = Depends(get_db)):
    """从回收站恢复；恢复后索引状态若落后当前版本则标记待重建（P2 启动补偿/后台索引接管）"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    if doc.deleted_at is None:
        return ApiResponse.error(msg="文档不在回收站", code=400)

    doc.deleted_at = None
    # 恢复后重新校验索引：版本不一致或失败 → 置为待索引
    if doc.index_state and (
        doc.index_state.indexed_revision != doc.current_revision
        or doc.index_state.status == 2
    ):
        doc.index_state.status = 0
    try:
        with GATE.write_section("恢复"):
            db.commit()
    except BackupInProgress as e:
        return ApiResponse.error(msg=str(e), code=409)
    logger.info(f"文档 {doc_id} 已从回收站恢复")
    return ApiResponse.success(msg=f"文档已恢复: ID={doc_id}")


@router.delete("/{doc_id}/permanent", response_model=ApiResponse)
def permanent_delete_document(doc_id: int, db: Session = Depends(get_db)):
    """
    永久删除：仅回收站条目可执行。
    安全约束：文件路径 resolve 后必须位于 UPLOAD_DIR 内（防目录穿越）；
    原件删除失败时不删库记录、不返回完全成功，保留待清理信息。
    """
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)
    if doc.deleted_at is None:
        return ApiResponse.error(
            msg="仅回收站中的文档可以永久删除（请先删除再执行本操作）", code=400)

    # 先处理原件：失败则不继续，避免"库已删、文件残留且无从追查"
    file_deleted = True
    file_note = ""
    try:
        fp = Path(doc.file_path).resolve()
        root = Path(UPLOAD_DIR).resolve()
        if root not in fp.parents and fp != root:
            return ApiResponse.error(
                msg="原件路径不在本应用存储目录内，已拒绝删除（疑似异常数据）", code=400)
        if fp.exists():
            fp.unlink()
            file_note = "原件已删除"
        else:
            file_note = "原件已不存在（仅删除记录）"
    except Exception as e:
        file_deleted = False
        logger.warning(f"永久删除文档 {doc_id}：原件删除失败：{e}")

    if not file_deleted:
        return ApiResponse.error(
            msg="原件删除失败，已保留数据库记录，请检查文件占用后重试", code=500)

    try:
        with GATE.write_section("永久删除"):
            db.delete(doc)  # extracts/chunks/index_state/revisions 由 ORM 级联删除
            db.commit()
    except BackupInProgress as e:
        return ApiResponse.error(msg=str(e), code=409)
    logger.info(f"文档 {doc_id} 已永久删除（{file_note}）")
    return ApiResponse.success(msg=f"文档已永久删除: ID={doc_id}（{file_note}）")
