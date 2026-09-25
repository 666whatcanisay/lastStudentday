"""
Pydantic 数据模型（请求/响应 Schema）
统一接口返回 JSON 格式: {"code":状态码, "msg":"提示信息", "data":业务数据}

方案 B 约定：
- 列表响应只带摘要（无全文、无绝对路径），全文与原件 URL 走详情接口
- 编辑接口以 expected_revision 做乐观并发控制，409 不覆盖
"""
from typing import Optional, List, Any, Dict
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


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
        from fastapi.encoders import jsonable_encoder
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=code, content=jsonable_encoder(
            {"code": code, "msg": msg, "data": data}))


# ==================== 抽取字段模型 ====================
class ExtractFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    doc_id: Optional[int] = None
    field_name: str
    field_value: Optional[str] = None
    field_key: Optional[str] = None

# ==================== 文档模型 ====================
class DocumentOut(BaseModel):
    """文档详情响应模型（含全文，列表请勿使用）"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_name: str
    file_path: Optional[str] = None  # 详情内部使用，不向前端输出绝对路径（见 to_detail_dict）
    file_type: str
    upload_time: datetime
    ocr_raw_text: Optional[str] = None
    infer_cost: Optional[float] = None
    status: int
    error_msg: Optional[str] = None
    extracts: List[ExtractFieldOut] = Field(default_factory=list)


class DocumentListItemOut(BaseModel):
    """列表项（摘要）：不含全文与绝对路径"""
    id: int
    file_name: str
    file_type: str
    upload_time: datetime
    doc_type: str = "other"
    status: int
    error_msg: Optional[str] = None
    infer_cost: Optional[float] = None
    current_revision: Optional[int] = None
    reviewed: bool = False
    deleted_at: Optional[datetime] = None
    engine_type: Optional[str] = None
    last_attempt_status: Optional[int] = None
    last_attempt_error: Optional[str] = None
    index_status: Optional[int] = None
    indexed_revision: Optional[int] = None
    extracts: List[ExtractFieldOut] = Field(default_factory=list)


class DocumentListOut(BaseModel):
    """文档列表分页响应"""
    total: int
    page: int
    page_size: int
    items: List[Any]


class DocumentPatchRequest(BaseModel):
    """编辑文档（乐观并发：expected_revision 不匹配返回 409）"""
    expected_revision: int
    doc_type: Optional[str] = None
    effective_text: Optional[str] = None
    fields: Optional[Dict[str, Optional[str]]] = None  # 稳定键 → 规范值
    mark_reviewed: bool = False


class RevisionSummaryOut(BaseModel):
    """版本历史摘要"""
    revision: int
    change_reason: str
    doc_type: str
    engine_type: Optional[str] = None
    created_at: datetime
    is_current: bool = False
    is_reviewed: bool = False


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
    extracts: List[ExtractFieldOut] = Field(default_factory=list)
    error_msg: Optional[str] = None
