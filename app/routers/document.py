"""
文档管理路由
  GET    /api/document/stats      获取文档统计数据（总数、成功数、平均推理耗时）
  GET    /api/document/list       分页获取文档列表，支持关键字检索
  GET    /api/document/{doc_id}   获取单份文档详情
  GET    /api/document/export     批量导出文档识别数据 Excel
  DELETE /api/document/{doc_id}   删除文档及关联抽取字段
"""
import io
import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_, func

from app.database import get_db
from app.models import DocumentInfo, DocumentExtract
from app.schemas import ApiResponse, DocumentOut, ExtractFieldOut

router = APIRouter(prefix="/api/document", tags=["文档管理"])
logger = logging.getLogger(__name__)


@router.get("/stats", response_model=ApiResponse)
async def get_document_stats(db: Session = Depends(get_db)):
    """
    获取文档统计数据接口
    - 文档总数
    - 识别成功数
    - 平均推理耗时(ms)
    """
    total = db.query(func.count(DocumentInfo.id)).scalar() or 0
    success_count = db.query(func.count(DocumentInfo.id)).filter(DocumentInfo.status == 1).scalar() or 0
    avg_cost = db.query(func.avg(DocumentInfo.infer_cost)).filter(
        DocumentInfo.status == 1, DocumentInfo.infer_cost != None
    ).scalar()
    avg_cost = round(avg_cost, 1) if avg_cost else 0.0

    return ApiResponse.success(data={
        "total": total,
        "success": success_count,
        "avg_cost": avg_cost,
    })


@router.get("/list", response_model=ApiResponse)
async def get_document_list(
    keyword: Optional[str] = Query(None, description="搜索关键字（文件名或识别文本）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数"),
    db: Session = Depends(get_db),
):
    """
    分页获取全部文档列表接口
    - 支持按文件名、识别文本关键字模糊检索
    - 按上传时间倒序排列
    """
    query = db.query(DocumentInfo)

    if keyword:
        query = query.filter(
            or_(
                DocumentInfo.file_name.contains(keyword),
                DocumentInfo.ocr_raw_text.contains(keyword),
            )
        )

    # 总数
    total = query.count()

    # 分页查询（按上传时间倒序）
    docs = (
        query.order_by(DocumentInfo.upload_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = []
    for doc in docs:
        extracts = [
            ExtractFieldOut(id=e.id, doc_id=e.doc_id, field_name=e.field_name, field_value=e.field_value)
            for e in doc.extracts
        ]
        items.append(
            DocumentOut(
                id=doc.id,
                file_name=doc.file_name,
                file_path=doc.file_path,
                file_type=doc.file_type,
                upload_time=doc.upload_time,
                ocr_raw_text=doc.ocr_raw_text,
                infer_cost=doc.infer_cost,
                status=doc.status,
                error_msg=doc.error_msg,
                extracts=extracts,
            )
        )

    return ApiResponse.success(
        data={
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [item.model_dump() for item in items],
        }
    )


@router.get("/export", response_model=None)
async def export_documents(
    doc_ids: str = Query(..., description="文档ID数组，逗号分隔，如 1,2,3"),
    db: Session = Depends(get_db),
):
    """
    批量导出文档识别数据 Excel 接口
    导出内容：文件名、上传时间、识别状态、推理耗时、原始识别文本、单据编号、日期、金额
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    # 解析文档ID列表
    id_list = [int(x.strip()) for x in doc_ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return ApiResponse.error(msg="请提供有效的文档ID", code=400)

    docs = db.query(DocumentInfo).filter(DocumentInfo.id.in_(id_list)).all()

    # 创建 Excel 工作簿
    wb = Workbook()
    ws = wb.active
    ws.title = "文档识别结果"

    # 表头样式
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="409EFF", end_color="409EFF", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center")

    # 写入表头
    headers = ["序号", "文件名", "文件类型", "上传时间", "识别状态", "推理耗时(ms)", "单据编号", "日期", "金额", "原始识别文本"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    # 状态映射
    status_map = {0: "待识别", 1: "识别成功", 2: "识别失败"}

    # 写入数据
    for idx, doc in enumerate(docs, 1):
        # 提取结构化字段
        field_map = {e.field_name: e.field_value for e in doc.extracts}
        row_data = [
            idx,
            doc.file_name,
            doc.file_type,
            doc.upload_time.strftime("%Y-%m-%d %H:%M:%S") if doc.upload_time else "",
            status_map.get(doc.status, "未知"),
            doc.infer_cost if doc.infer_cost else "",
            field_map.get("单据编号", ""),
            field_map.get("日期", ""),
            field_map.get("金额", ""),
            doc.ocr_raw_text or "",
        ]
        for col, value in enumerate(row_data, 1):
            ws.cell(row=idx + 1, column=col, value=value)

    # 设置列宽
    col_widths = [6, 25, 10, 20, 10, 14, 20, 15, 15, 60]
    for i, width in enumerate(col_widths, 1):
        ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = width

    # 导出到内存
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    # URL 编码中文文件名
    from urllib.parse import quote
    filename = f"文档识别结果导出_{len(docs)}条.xlsx"
    encoded_filename = quote(filename)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
    )


@router.get("/{doc_id}", response_model=ApiResponse)
async def get_document_detail(doc_id: int, db: Session = Depends(get_db)):
    """获取单份文档详情接口，返回原始文本、全部抽取结构化字段"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)

    extracts = [
        ExtractFieldOut(id=e.id, doc_id=e.doc_id, field_name=e.field_name, field_value=e.field_value)
        for e in doc.extracts
    ]

    doc_out = DocumentOut(
        id=doc.id,
        file_name=doc.file_name,
        file_path=doc.file_path,
        file_type=doc.file_type,
        upload_time=doc.upload_time,
        ocr_raw_text=doc.ocr_raw_text,
        infer_cost=doc.infer_cost,
        status=doc.status,
        error_msg=doc.error_msg,
        extracts=extracts,
    )

    return ApiResponse.success(data=doc_out.model_dump())


@router.delete("/{doc_id}", response_model=ApiResponse)
async def delete_document(doc_id: int, db: Session = Depends(get_db)):
    """删除文档及关联抽取字段数据接口"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={doc_id}", code=404)

    # 删除本地文件
    import os
    from pathlib import Path
    file_path = Path(doc.file_path)
    if file_path.exists():
        try:
            file_path.unlink()
            logger.info(f"已删除本地文件: {file_path}")
        except Exception as e:
            logger.warning(f"删除本地文件失败: {e}")

    # 删除数据库记录（关联的 document_extract 通过 cascade 自动删除）
    db.delete(doc)
    db.commit()

    return ApiResponse.success(msg=f"文档删除成功: ID={doc_id}")
