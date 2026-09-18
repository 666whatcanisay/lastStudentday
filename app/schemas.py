"""
Pydantic 数据模型（请求/响应 Schema）
统一接口返回 JSON 格式: {"code":状态码, "msg":"提示信息", "data":业务数据}
"""
from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel


# ==================== 统一响应模型 ====================
class ApiResponse(BaseModel):
    """统一 API 响应格式"""
    code: int = 200
    msg: str = "success"
    data: Optional[Any] = None

    @classmethod
    def success(cls, data: Any = None, msg: str = "success"):
        return cls(code=200, msg=msg, data=data)

    @classmethod
    def error(cls, msg: str = "error", code: int = 500, data: Any = None):
        return cls(code=code, msg=msg, data=data)


# ==================== 抽取字段模型 ====================
class ExtractFieldOut(BaseModel):
    id: int
    doc_id: int
    field_name: str
    field_value: Optional[str] = None

    class Config:
        from_attributes = True


# ==================== 文档模型 ====================
class DocumentOut(BaseModel):
    """文档列表/详情响应模型"""
    id: int
    file_name: str
    file_path: str
    file_type: str
    upload_time: datetime
    ocr_raw_text: Optional[str] = None
    infer_cost: Optional[float] = None
    status: int
    error_msg: Optional[str] = None
    extracts: List[ExtractFieldOut] = []

    class Config:
        from_attributes = True


class DocumentListOut(BaseModel):
    """文档列表分页响应"""
    total: int
    page: int
    page_size: int
    items: List[DocumentOut]


class UploadResponse(BaseModel):
    """文件上传响应"""
    doc_id: int
    file_name: str
    file_type: str
    file_path: str


class OcrRecognizeResponse(BaseModel):
    """OCR 识别响应"""
    doc_id: int
    status: int
    ocr_raw_text: Optional[str] = None
    infer_cost: Optional[float] = None
    extracts: List[ExtractFieldOut] = []
    error_msg: Optional[str] = None
