"""
OCR 文字识别路由
POST /api/ocr/recognize — 传入文档ID，执行图像预处理+OCR识别+字段抽取
"""
import logging
import numpy as np
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DocumentInfo, DocumentExtract
from app.schemas import ApiResponse
from app.services.preprocess import load_image_from_path, preprocess_image
from app.services.ocr_engine import recognize, get_engine_info
from app.services.extractor import extract_fields
from app.services.pdf_handler import pdf_to_images

router = APIRouter(prefix="/api/ocr", tags=["OCR识别"])
logger = logging.getLogger(__name__)


class OcrRequest(BaseModel):
    doc_id: int


@router.post("/recognize", response_model=ApiResponse)
def recognize_document(req: OcrRequest, db: Session = Depends(get_db)):
    """
    执行文档 OCR 识别接口
    流程: 加载文件 → 图像预处理 → 轻量化OCR推理 → 文本字段抽取 → 存入数据库
    """
    # 1. 查询文档记录
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == req.doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={req.doc_id}", code=404)

    # 2. 检查文件是否存在
    file_path = Path(doc.file_path)
    if not file_path.exists():
        doc.status = 2
        doc.error_msg = "原始文件不存在"
        db.commit()
        return ApiResponse.error(msg="原始文件不存在", code=404)

    try:
        logger.info(f"=== 开始识别文档 ID={doc.id} 文件={doc.file_name} ===")

        all_text_lines = []
        total_infer_cost = 0.0

        if doc.file_type == "pdf":
            # PDF: 逐页渲染为图像后识别
            images = pdf_to_images(str(file_path))
            if not images:
                raise ValueError("PDF 解析失败，未获取到页面图像")

            for i, img in enumerate(images):
                logger.info(f"--- 处理 PDF 第 {i+1}/{len(images)} 页 ---")
                # 图像预处理
                processed = preprocess_image(img)
                # OCR 识别
                text, cost, _ = recognize(processed)
                all_text_lines.append(f"=== 第 {i+1} 页 ===\n{text}")
                total_infer_cost += cost
        else:
            # 图片: 直接加载识别
            img = load_image_from_path(str(file_path))
            # 图像预处理（轻量化优化核心）
            processed = preprocess_image(img)
            # OCR 识别
            text, cost, _ = recognize(processed)
            all_text_lines.append(text)
            total_infer_cost = cost

        # 3. 拼接完整识别文本
        full_text = "\n".join(all_text_lines)
        logger.info(f"识别完成，文本长度={len(full_text)}，推理耗时={total_infer_cost:.2f}ms")

        # 4. 文本结构化字段抽取
        fields = extract_fields(full_text)

        # 5. 清除旧的抽取记录（如有）
        db.query(DocumentExtract).filter(DocumentExtract.doc_id == doc.id).delete()

        # 6. 保存抽取字段
        for field in fields:
            extract = DocumentExtract(
                doc_id=doc.id,
                field_name=field["field_name"],
                field_value=field["field_value"],
            )
            db.add(extract)

        # 7. 更新文档主表
        doc.ocr_raw_text = full_text
        doc.infer_cost = round(total_infer_cost, 2)
        doc.status = 1  # 识别成功
        doc.error_msg = None
        db.commit()
        db.refresh(doc)

        # 8. 构建响应
        from app.schemas import ExtractFieldOut
        extract_list = [
            ExtractFieldOut(id=e.id, doc_id=e.doc_id, field_name=e.field_name, field_value=e.field_value)
            for e in doc.extracts
        ]

        # 如实标注引擎模式。
        # 背景：RapidOCR 加载失败时会降级为 Mock 引擎，返回的是**占位假文本**，
        # 但流程本身没出错 —— 旧实现一律回 "OCR识别成功"，于是文档列表和导出的
        # Excel 里都记成「识别成功」，只看列表根本察觉不到。
        # 顶栏虽然会显示「模拟引擎 (Mock)」warning 标签，但那是全局状态、
        # 且导出成 Excel 后就完全丢失了上下文。
        engine_info = get_engine_info()
        is_mock = not engine_info.get("optimized", True)
        msg = (
            "OCR识别完成（当前为模拟引擎 Mock，以上文本为占位内容，非真实识别结果）"
            if is_mock else "OCR识别成功"
        )
        if is_mock:
            logger.warning(
                f"文档 ID={doc.id} 由 Mock 引擎处理，结果为占位文本（RapidOCR 未加载成功）"
            )

        return ApiResponse.success(
            data={
                "doc_id": doc.id,
                "status": doc.status,
                "ocr_raw_text": doc.ocr_raw_text,
                "infer_cost": doc.infer_cost,
                "engine_optimized": engine_info.get("optimized", True),
                "engine_name": engine_info.get("name"),
                "extracts": [e.model_dump() for e in extract_list],
            },
            msg=msg,
        )

    except Exception as e:
        logger.error(f"OCR识别失败: {e}", exc_info=True)
        doc.status = 2
        doc.error_msg = str(e)[:500]
        db.commit()
        return ApiResponse.error(msg=f"OCR识别失败: {str(e)}", code=500)


@router.get("/engine-info", response_model=ApiResponse)
def get_ocr_engine_info():
    """获取当前 OCR 引擎信息（用于前端展示引擎状态）"""
    info = get_engine_info()
    return ApiResponse.success(data=info)
