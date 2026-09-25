"""
OCR 文字识别路由
POST /api/ocr/recognize — 传入文档ID，执行图像预处理+OCR识别+字段抽取

方案 B：
- 识别闭环在 services/recognition.py（临时结果→原子发布版本，失败不动旧版本）
- 识别成功后经 BackgroundTasks 自动建立/更新 RAG 索引（独立 Session，失败不影响主流程）
- force=true 表示用户已在界面确认重新识别
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DocumentInfo
from app.schemas import ApiResponse
from app.services.ocr_engine import get_engine_info
from app.services.recognition import (
    RecognitionError, record_attempt_failure, run_recognition,
)
from app.services.pdf_handler import PdfError
from app.services.backup_gate import BackupInProgress

router = APIRouter(prefix="/api/ocr", tags=["OCR识别"])
logger = logging.getLogger(__name__)


class OcrRequest(BaseModel):
    doc_id: int
    force: bool = False  # 已有有效版本时必须为 true（界面已确认重新识别）


@router.post("/recognize", response_model=ApiResponse)
def recognize_document(req: OcrRequest, background: BackgroundTasks,
                       db: Session = Depends(get_db)):
    """执行文档 OCR 识别（原子版本发布；成功自动触发后台索引）"""
    doc = db.query(DocumentInfo).filter(DocumentInfo.id == req.doc_id).first()
    if not doc:
        return ApiResponse.error(msg=f"文档不存在: ID={req.doc_id}", code=404)
    if doc.deleted_at is not None:
        return ApiResponse.error(msg="文档在回收站中，请先恢复再识别", code=400)

    try:
        result = run_recognition(db, doc, force=req.force)
    except BackupInProgress as e:
        record_attempt_failure(db, doc, str(e))
        return ApiResponse.error(msg=str(e), code=409)
    except PdfError as e:
        record_attempt_failure(db, doc, str(e))
        return ApiResponse.error(msg=str(e), code=400)
    except RecognitionError as e:
        record_attempt_failure(db, doc, str(e))
        return ApiResponse.error(msg=str(e), code=e.code)

    # 识别成功 → 后台自动建索引（独立 Session；失败只影响索引状态，不影响本次识别结果）
    from app.services.rag_indexer import index_document_background
    background.add_task(index_document_background, doc.id)

    engine_info = get_engine_info()
    return ApiResponse.success(
        data={
            "doc_id": result["doc_id"],
            "status": result["status"],
            "revision": result["revision"],
            "doc_type": result["doc_type"],
            "ocr_raw_text": result["ocr_raw_text"],
            "infer_cost": result["infer_cost"],
            "engine_type": engine_info.get("engine_type"),
            "engine_name": engine_info.get("name"),
            "extracts": [
                {"field_key": f["field_key"], "field_name": f["field_name"],
                 "field_value": f["field_value"], "field_value_raw": f["field_value_raw"]}
                for f in result["fields"]
            ],
        },
        msg="OCR识别成功",
    )


@router.get("/engine-info", response_model=ApiResponse)
def get_ocr_engine_info():
    """获取当前 OCR 引擎信息（用于前端展示引擎状态）"""
    info = get_engine_info()
    return ApiResponse.success(data=info)
