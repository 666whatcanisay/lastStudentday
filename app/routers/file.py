"""
文档文件上传路由
POST /api/file/upload — 接收图片/PDF文件，返回文件存储信息
"""
import os
import uuid
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import UPLOAD_DIR, ALLOWED_EXTENSIONS, MAX_FILE_SIZE
from app.database import get_db
from app.models import DocumentInfo
from app.schemas import ApiResponse, UploadResponse

router = APIRouter(prefix="/api/file", tags=["文件上传"])
logger = logging.getLogger(__name__)


@router.post("/upload", response_model=ApiResponse)
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    文档文件上传接口
    - 支持 JPG、PNG、BMP 图片、PDF 文件
    - 文件大小校验、格式校验
    - 返回文件存储信息与文档ID
    """
    # 1. 文件格式校验
    original_name = file.filename or "unknown"
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return ApiResponse.error(
            msg=f"不支持的文件格式: {ext}，仅支持 {', '.join(ALLOWED_EXTENSIONS)}",
            code=400,
        )

    # 2. 读取文件内容并校验大小
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        return ApiResponse.error(
            msg=f"文件大小超过限制({MAX_FILE_SIZE // 1024 // 1024}MB)",
            code=400,
        )

    if len(file_bytes) == 0:
        return ApiResponse.error(msg="文件内容为空，可能已损坏", code=400)

    # 3. 生成唯一文件名并保存
    unique_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    save_path = UPLOAD_DIR / unique_name

    try:
        with open(save_path, "wb") as f:
            f.write(file_bytes)
        logger.info(f"文件保存成功: {save_path}")
    except Exception as e:
        logger.error(f"文件保存失败: {e}")
        return ApiResponse.error(msg=f"文件保存失败: {str(e)}", code=500)

    # 4. 判断文件类型
    file_type = "pdf" if ext == ".pdf" else "image"

    # 5. 写入数据库
    doc = DocumentInfo(
        file_name=original_name,
        file_path=str(save_path),
        file_type=file_type,
        status=0,  # 待识别
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    return ApiResponse.success(
        data=UploadResponse(
            doc_id=doc.id,
            file_name=doc.file_name,
            file_type=doc.file_type,
            file_path=doc.file_path,
        ).model_dump(),
        msg="文件上传成功",
    )
