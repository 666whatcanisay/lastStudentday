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

方案 B（2026-09-22）新增：
  - document_revision 版本快照表（(doc_id, revision) 唯一），OCR/编辑/改类型
    每次有效变更产生一个不可变快照；document_info.current_revision 指向当前有效版本
  - document_info 增加类型/哈希/版本/校对/软删除/引擎来源/最近尝试状态
  - document_chunk 增加 revision + source_kind（正文分块与字段分块区分）
  - doc_index_state 增加 indexed_revision（索引与有效版本的对应关系）

⚠️ 级联删除注意：SQLite 默认不强制外键（见 database.py 的 PRAGMA 修复），
   因此所有父子关系都必须显式声明 ORM 层 cascade="all, delete-orphan"，
   不能只依赖 DDL 中的 ondelete="CASCADE"。

⚠️ schema 演进必须走 Alembic（alembic/ 目录），不再靠 create_all() 隐式变更。
"""
from sqlalchemy import (
    Column, Integer, String, Text, Float, DateTime, ForeignKey,
    LargeBinary, UniqueConstraint, func,
)
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

    # ---- 方案 B 扩展列 ----
    doc_type = Column(String(16), nullable=False, default="other", server_default="other",
                      comment="文档类型：invoice发票/contract合同/other其他；本地规则辅助分类，用户可覆盖")
    file_sha256 = Column(String(64), nullable=True, unique=True,
                         comment="原件内容 SHA-256；完全相同内容不新增（旧数据 NULL 不参与唯一）")
    current_revision = Column(Integer, nullable=True,
                              comment="当前有效版本号；NULL=从未有有效识别结果（首败）")
    reviewed_revision = Column(Integer, nullable=True,
                               comment="已校对版本号；等于 current_revision 即视为已校对，变更后清空")
    deleted_at = Column(DateTime, nullable=True, comment="软删除时间；非 NULL 即在回收站")
    engine_type = Column(String(16), nullable=True,
                         comment="当前有效版本的引擎来源：rapidocr/tesseract/mock/legacy_unknown")
    last_attempt_status = Column(Integer, nullable=True,
                                 comment="最近一次识别尝试状态（与 status 语义一致），失败不影响旧有效版本")
    last_attempt_error = Column(String(500), nullable=True, comment="最近一次识别尝试的错误信息")

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

    # 一对多关联：版本快照（方案 B）
    revisions = relationship(
        "DocumentRevision",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentRevision.revision",
    )


class DocumentExtract(Base):
    """结构化抽取字段明细表 document_extract"""
    __tablename__ = "document_extract"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="抽取字段唯一ID")
    doc_id = Column(Integer, ForeignKey("document_info.id", ondelete="CASCADE"), nullable=False, comment="关联所属文档ID")
    field_name = Column(String(100), nullable=False, comment="抽取字段名称（中文标签，由字段注册表提供）")
    field_value = Column(String(500), nullable=True, comment="规范值（日期 ISO / 金额十进制字符串 / 文本原文）")
    # ---- 方案 B 扩展列 ----
    field_key = Column(String(64), nullable=True,
                       comment="稳定字段键（如 invoice_number/total_amount），注册表驱动，替代中文名硬编码")
    field_value_raw = Column(String(500), nullable=True, comment="抽取时的原始值（规范化前），人工编辑后为空")
    revision = Column(Integer, nullable=True, comment="该字段所属的有效版本号")

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
    # ---- 方案 B 扩展列 ----
    revision = Column(Integer, nullable=True, comment="该分块所属的有效版本号，用于版本级索引替换与校验")
    source_kind = Column(String(8), nullable=True, default="text",
                         comment="分块来源：text=有效全文分块；fields=当前字段的结构化标签分块")

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
    # ---- 方案 B 扩展列 ----
    indexed_revision = Column(Integer, nullable=True,
                              comment="已索引的有效版本号；与 document_info.current_revision 不一致即需重建")

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


# ==================== 方案 B：版本快照表 ====================

class DocumentRevision(Base):
    """
    文档版本快照表 document_revision

    每次有效变更（OCR 成功 / 人工编辑 / 切换类型 / 旧库迁移）产生一条不可变快照，
    (doc_id, revision) 唯一。document_info.current_revision 指向当前有效版本。

    设计要点：
    - ocr_raw_text 与 effective_text 分开存：人工编辑只改有效文本，绝不覆盖 OCR 原始结果
    - fields_json 是该版本全部有效字段的稳定键→规范值快照（含原始抽取值），
      历史展示不需要再关联 document_extract 反推
    - 本表只追加不修改；历史回滚本版不做（任务书 3.1）
    """
    __tablename__ = "document_revision"
    __table_args__ = (
        UniqueConstraint("doc_id", "revision", name="uq_document_revision_doc_rev"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="快照唯一ID")
    doc_id = Column(Integer, ForeignKey("document_info.id", ondelete="CASCADE"),
                    nullable=False, index=True, comment="关联所属文档ID")
    revision = Column(Integer, nullable=False, comment="版本号，从1开始递增")
    change_reason = Column(String(32), nullable=False,
                           comment="变更原因：ocr首次/重新识别、edit人工编辑、retype切换类型、legacy旧库迁移")
    doc_type = Column(String(16), nullable=False, default="other",
                      comment="该版本的文档类型")
    ocr_raw_text = Column(Text, nullable=True, comment="OCR 原始识别文本（该版本时点，人工编辑不改变它）")
    effective_text = Column(Text, nullable=True, comment="有效全文（统计/索引/导出使用）")
    fields_json = Column(Text, nullable=True,
                         comment="有效字段快照 JSON：[{field_key,field_name,field_value,field_value_raw}]")
    engine_type = Column(String(16), nullable=True,
                         comment="该版本的引擎来源：rapidocr/tesseract/mock/legacy_unknown")
    created_at = Column(DateTime, nullable=False, server_default=func.now(), comment="快照创建时间")

    document = relationship("DocumentInfo", back_populates="revisions")
