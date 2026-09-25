"""旧库基线（方案 B 之前的 create_all 结构）

Revision ID: 0001_legacy
Revises:
Create Date: 2026-09-22

本迁移精确复刻方案 B 之前 `Base.metadata.create_all()` 产出的五张旧表结构，
作为存量数据库的 stamp 基线：
  - 新库：upgrade 时会先执行本迁移建旧表，再由 0002 升级到新结构
  - 旧库：database.init_database() 核对结构匹配后 stamp 本版本，再 upgrade 到 head

⚠️ 不要修改本文件 —— 它是历史结构的定格，改动会让旧库 stamp 后的结构对不上。
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_legacy"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_info",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("upload_time", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("ocr_raw_text", sa.Text(), nullable=True),
        sa.Column("infer_cost", sa.Float(), nullable=True),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("error_msg", sa.String(500), nullable=True),
    )
    op.create_table(
        "document_extract",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("doc_id", sa.Integer(),
                  sa.ForeignKey("document_info.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_name", sa.String(100), nullable=False),
        sa.Column("field_value", sa.String(500), nullable=True),
    )
    op.create_table(
        "document_chunk",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("doc_id", sa.Integer(),
                  sa.ForeignKey("document_info.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("chunk_text_raw", sa.Text(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embed_dim", sa.Integer(), nullable=True),
        sa.Column("embed_model", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "doc_index_state",
        sa.Column("doc_id", sa.Integer(),
                  sa.ForeignKey("document_info.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("embed_model", sa.String(64), nullable=True),
        sa.Column("error_msg", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "qa_record",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("rewritten_q", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("hit_chunk_ids", sa.String(500), nullable=True),
        sa.Column("top_score", sa.Float(), nullable=True),
        sa.Column("fusion_mode", sa.String(16), nullable=True),
        sa.Column("llm_model", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("qa_record")
    op.drop_table("doc_index_state")
    op.drop_table("document_chunk")
    op.drop_table("document_extract")
    op.drop_table("document_info")
