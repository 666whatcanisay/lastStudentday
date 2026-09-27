"""Document export routes. Each request captures immutable revision snapshots."""

import io
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DocumentInfo
from app.schemas import ApiResponse
from app.services import exporter
from app.services import revisions as rev_svc

router = APIRouter(prefix="/api/document", tags=["文档导出"])
logger = logging.getLogger(__name__)

# ==================== 导出（Excel / PDF 报告 / 批量 ZIP） ====================

def _parse_doc_ids(doc_ids: str) -> List[int]:
    ids = []
    for x in (doc_ids or "").split(","):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    return ids


def _prepare_export_items(db: Session, doc_ids: List[int]) -> List[Dict]:
    """
    请求开始时空一次性锁定每份文档的**有效版本快照**。

    这是「正文和字段不能在生成途中混用不同版本」的实现方式：
    快照在这里取出来，后续 PDF/Excel 只读这份内存数据，不再回查数据库。
    """
    items: List[Dict] = []
    for did in doc_ids:
        doc = db.query(DocumentInfo).filter(DocumentInfo.id == did).first()
        entry: Dict = {"doc_id": did, "doc": doc}
        if not doc:
            entry["error"] = "文档不存在"
        elif doc.deleted_at is not None:
            entry["error"] = "文档在回收站，不可导出"
        else:
            snap = rev_svc.get_effective_snapshot(db, doc.id)
            if not snap:
                entry["error"] = "该文档没有有效版本，无法导出"
            else:
                entry["snap"] = snap
                entry["fields"] = rev_svc.snapshot_fields(snap)
        items.append(entry)
    return items


def _export_rows(items: List[Dict]) -> List[Dict]:
    """导出项 → Excel 行（同样只吃锁定的快照）"""
    rows = []
    for it in items:
        if it.get("error") or not it.get("snap"):
            continue
        doc, snap = it["doc"], it["snap"]
        rows.append({
            "doc_id": doc.id,
            "file_name": doc.file_name,
            "file_type": doc.file_type,
            "upload_time": doc.upload_time,
            "doc_type": snap.doc_type or "other",
            "revision": snap.revision,
            "reviewed": bool(doc.current_revision is not None
                             and doc.reviewed_revision == doc.current_revision),
            "fields": it["fields"],
            "effective_text": snap.effective_text,
        })
    return rows


def _attachment(filename: str):
    from urllib.parse import quote
    return f"attachment; filename*=UTF-8''{quote(filename)}"


@router.get("/export", response_model=None)
def export_documents(
    doc_ids: str = Query(..., description="文档ID数组，逗号分隔，如 1,2,3"),
    db: Session = Depends(get_db),
):
    """
    导出 Excel：按类型分工作表（发票/合同/其他，按需生成），
    金额为数值单元格、币种独立列，文本按字符串写入（防公式注入）。
    """
    id_list = _parse_doc_ids(doc_ids)
    if not id_list:
        return ApiResponse.error(msg="请提供有效的文档ID", code=400)

    items = _prepare_export_items(db, id_list)
    rows = _export_rows(items)
    if not rows:
        failed = "；".join(f"{i['doc_id']}: {i['error']}" for i in items if i.get("error"))
        return ApiResponse.error(msg=f"没有可导出的有效文档（{failed or '无有效版本'}）", code=400)

    data = exporter.build_excel_workbook(rows=rows)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _attachment(f"文档识别结果导出_{len(rows)}条.xlsx")},
    )


@router.get("/export/report", response_model=None)
def export_single_report(
    doc_id: int = Query(..., description="文档ID"),
    attach_original: bool = Query(False, description="是否附加原件（缺失则该份导出失败）"),
    background: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """单份 PDF 识别结果报告（A4，含字段/有效全文/版本/页码，可选附原件）"""
    items = _prepare_export_items(db, [doc_id])
    it = items[0]
    if it.get("error"):
        return ApiResponse.error(msg=it["error"], code=400)

    work_root = exporter.make_work_root()
    tmp_dir = Path(tempfile.mkdtemp(prefix=exporter.EXPORT_PREFIX, dir=str(work_root)))
    try:
        data = exporter.build_report_pdf(
            doc=it["doc"], snap=it["snap"], fields=it["fields"],
            attach_original=attach_original, tmp_dir=tmp_dir)
    except exporter.ExportError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return ApiResponse.error(msg=str(e), code=400)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.error(f"文档 {doc_id} 报告导出失败: {e}", exc_info=True)
        return ApiResponse.error(msg=f"报告生成失败：{str(e)[:200]}", code=500)
    finally:
        if background is not None:
            background.add_task(shutil.rmtree, str(tmp_dir), True)

    name = f"doc_{doc_id}_r{it['snap'].revision}_报告.pdf"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": _attachment(name)},
    )


@router.get("/export/reports", response_model=None)
def export_batch_reports(
    doc_ids: str = Query(..., description="文档ID数组，逗号分隔；最多 20 份"),
    attach_original: bool = Query(False, description="是否附加原件"),
    db: Session = Depends(get_db),
):
    """
    批量导出：一份一个 PDF + export_manifest.json（记录每份 revision 与失败原因）。
    部分成功可下载；全部失败返回错误，不产出空 ZIP。
    """
    id_list = _parse_doc_ids(doc_ids)
    if not id_list:
        return ApiResponse.error(msg="请提供有效的文档ID", code=400)
    if len(id_list) > exporter.MAX_BATCH:
        return ApiResponse.error(
            msg=f"单次最多导出 {exporter.MAX_BATCH} 份，当前 {len(id_list)} 份，请分批导出",
            code=400)

    items = _prepare_export_items(db, id_list)
    data, manifest = exporter.build_reports_zip(
        items=items, attach_original=attach_original,
        work_root=exporter.make_work_root())

    if manifest["success_count"] == 0:
        reasons = "；".join(
            f"#{m['doc_id']} {m['reason']}" for m in manifest["items"])
        return ApiResponse.error(
            msg=f"全部 {manifest['failed_count']} 份导出失败：{reasons[:600]}", code=400)

    name = f"文档报告批量导出_{manifest['success_count']}份.zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={
            "Content-Disposition": _attachment(name),
            # 失败份数直接放响应头，前端不用解包就能提示
            "X-Export-Failed": str(manifest["failed_count"]),
        },
    )
