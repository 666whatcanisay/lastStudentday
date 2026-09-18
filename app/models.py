"""
SQLAlchemy ORM 数据模型
对应数据库设计文档的两张表：
  - document_info   文档信息主表
  - document_extract 结构化抽取字段明细表
一对多关联关系
"""
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship

from app.database import Base


class DocumentInfo(Base):
    """文档信息主表 document_info"""
    __tablename__ = "document_info"

    # SQLite 使用 INTEGER PRIMARY KEY 实现自增（等效 bigint）
    id = Column(Integer, primary_key=True, autoincrement=True, comment="文档唯一标识ID")
    file_name = Column(String(255), nullable=False, comment="用户上传原始文件名称")
    file_path = Column(String(512), nullable=False, comment="文件本地存储绝对路径")
    file_type = Column(String(32), nullable=False, comment="文件类型：image图片/pdf文档")
    upload_time = Column(DateTime, nullable=False, server_default=func.now(), comment="文档上传系统时间")
    ocr_raw_text = Column(Text, nullable=True, comment="OCR完整原始识别文本")
    infer_cost = Column(Float, nullable=True, comment="模型推理耗时，单位ms")
    status = Column(Integer, nullable=False, default=0, comment="文档状态：0待识别、1识别成功、2识别失败")
    error_msg = Column(String(500), nullable=True, comment="识别失败时的错误信息")

    # 一对多关联：一条文档对应多条抽取字段
    extracts = relationship(
        "DocumentExtract",
        back_populates="document",
        cascade="all, delete-orphan",  # 删除文档时同步删除关联抽取字段
    )


class DocumentExtract(Base):
    """结构化抽取字段明细表 document_extract"""
    __tablename__ = "document_extract"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="抽取字段唯一ID")
    doc_id = Column(Integer, ForeignKey("document_info.id", ondelete="CASCADE"), nullable=False, comment="关联所属文档ID")
    field_name = Column(String(100), nullable=False, comment="抽取字段名称：单据编号、日期、金额")
    field_value = Column(String(500), nullable=True, comment="对应字段识别抽取内容")

    # 反向关联
    document = relationship("DocumentInfo", back_populates="extracts")
