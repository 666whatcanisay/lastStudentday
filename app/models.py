"""
SQLAlchemy ORM 数据模型
对应数据库设计文档的两张表：
  - document_info   文档信息主表
  - document_extract 结构化抽取字段明细表
一对多关联关系

RAG 功能新增三张表：
  - document_chunk   文档分块向量表（RAG 检索的最小单元）
  - doc_index_state  文档索引状态表（让索引过程可查询、可重试、可观测）
  - qa_record        问答记录表（问答历史与效果调试）

⚠️ 级联删除注意：SQLite 默认不强制外键（见 database.py 的 PRAGMA 修复），
   因此所有父子关系都必须显式声明 ORM 层 cascade="all, delete-orphan"，
   不能只依赖 DDL 中的 ondelete="CASCADE"。
"""
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, LargeBinary, func
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

    # 一对多关联：一条文档对应多个检索分块（RAG）
    chunks = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )

    # 一对一关联：一条文档对应一条索引状态（RAG）
    index_state = relationship(
        "DocIndexState",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
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


# ==================== RAG 相关表 ====================

class DocumentChunk(Base):
    """
    文档分块向量表 document_chunk

    RAG 检索的最小单元。每份文档经「归一化 → 语义分块 → 向量化」后
    产生若干条分块记录，检索时以分块为粒度召回，再回溯到所属文档。
    """
    __tablename__ = "document_chunk"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="分块唯一ID")
    doc_id = Column(Integer, ForeignKey("document_info.id", ondelete="CASCADE"),
                    nullable=False, index=True, comment="关联所属文档ID")
    chunk_index = Column(Integer, nullable=False, comment="该文档内的分块序号，从0开始")
    chunk_text = Column(Text, nullable=False, comment="归一化后的分块文本（用于检索与展示）")
    chunk_text_raw = Column(Text, nullable=True, comment="归一化前的原始分块文本（用于精确引用展示）")
    char_start = Column(Integer, nullable=True, comment="在 ocr_raw_text 中的起始字符偏移（前端高亮用，辅助手段）")
    char_end = Column(Integer, nullable=True, comment="在 ocr_raw_text 中的结束字符偏移")
    page_no = Column(Integer, nullable=True, comment="PDF页码；图片类型为NULL")
    embedding = Column(LargeBinary, nullable=True, comment="向量数据，float32 序列化后的二进制")
    embed_dim = Column(Integer, nullable=True, comment="向量维度，用于一致性校验")
    embed_model = Column(String(64), nullable=True, comment="生成该向量的模型标识，换模型时可定向重建")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), comment="分块创建时间")

    document = relationship("DocumentInfo", back_populates="chunks")


class DocIndexState(Base):
    """
    文档索引状态表 doc_index_state

    背景：索引由 BackgroundTasks 异步触发，是「尽力而为」的，进程重启即丢。
    本表把索引过程变成显式状态机，使「哪些文档未索引/索引失败」可查询、可重试。

    状态码与 document_info.status 语义保持一致（0待/1成功/2失败），降低认知成本。
    """
    __tablename__ = "doc_index_state"

    doc_id = Column(Integer, ForeignKey("document_info.id", ondelete="CASCADE"),
                    primary_key=True, comment="关联文档ID（一对一）")
    status = Column(Integer, nullable=False, default=0, comment="索引状态：0待索引、1索引成功、2索引失败")
    chunk_count = Column(Integer, nullable=True, comment="本次索引生成的分块数量")
    embed_model = Column(String(64), nullable=True, comment="本次索引使用的向量模型标识")
    error_msg = Column(String(500), nullable=True, comment="索引失败时的错误信息")
    updated_at = Column(DateTime, nullable=False, server_default=func.now(),
                        onupdate=func.now(), comment="状态最后更新时间")

    document = relationship("DocumentInfo", back_populates="index_state")


class QaRecord(Base):
    """
    问答记录表 qa_record

    用于问答历史展示、演示统计与效果调试。
    记录改写后的查询（rewritten_q）与双路排名，便于事后分析检索质量。
    """
    __tablename__ = "qa_record"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="问答记录ID")
    question = Column(Text, nullable=False, comment="用户原始问题")
    rewritten_q = Column(Text, nullable=True, comment="查询改写后的文本，便于调试")
    answer = Column(Text, nullable=True, comment="LLM生成的回答")
    hit_chunk_ids = Column(String(500), nullable=True, comment="命中的分块ID列表，逗号分隔")
    top_score = Column(Float, nullable=True, comment="最高融合得分")
    fusion_mode = Column(String(16), nullable=True, comment="使用的融合策略：rrf / weighted")
    llm_model = Column(String(64), nullable=True, comment="生成回答使用的LLM模型")
    latency_ms = Column(Float, nullable=True, comment="本次问答总耗时，单位ms")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), comment="提问时间")
