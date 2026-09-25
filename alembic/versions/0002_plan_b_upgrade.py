"""方案 B 升级：类型/版本/软删除/引擎来源 + document_revision 快照表 + 旧数据回填

Revision ID: 0002_plan_b
Revises: 0001_legacy
Create Date: 2026-09-22

结构变更：
  - document_info: +doc_type/file_sha256(UNIQUE)/current_revision/reviewed_revision/
                   deleted_at/engine_type/last_attempt_status/last_attempt_error
  - document_extract: +field_key/field_value_raw/revision
  - document_chunk: +revision/source_kind
  - doc_index_state: +indexed_revision
  - 新表 document_revision（(doc_id, revision) 唯一）

数据回填（旧库迁移规则，任务书 3.2）：
  - 每个 status=1 的旧文档创建 revision=1 快照（change_reason=legacy），
    保存当时能看到的文本和字段，不伪造更早历史
  - engine_type：能识别出 Mock 占位文本的标 mock（后续统计排除），
    其余标 legacy_unknown；一律未校对
  - doc_type 一律 other（旧库无类型信息，由用户或重新识别修正）
  - status!=1 的文档无有效结果，current_revision 保持 NULL
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0002_plan_b"
down_revision = "0001_legacy"
branch_labels = None
depends_on = None

# Mock 占位文本特征（app/services/ocr_engine.py 的 _recognize_mock 输出）
_MOCK_MARKERS = ("模拟OCR识别结果", "MOCK-20240101", "OCR引擎未安装")

# 旧三字段（中文名）→ 新稳定键（other 类型注册表）的迁移映射
_LEGACY_KEY_MAP = {
    "单据编号": "document_number",
    "日期": "document_date",
    # 旧「金额」在 other 类型注册表中没有对应键，保留 field_name 但不强行编造 field_key
    "金额": None,
}


def _is_mock_text(text: str | None) -> bool:
    if not text:
        return False
    return any(m in text for m in _MOCK_MARKERS)


def upgrade() -> None:
    # ---------- 结构变更 ----------
    with op.batch_alter_table("document_info") as b:
        b.add_column(sa.Column("doc_type", sa.String(16), nullable=False,
                               server_default="other"))
        b.add_column(sa.Column("file_sha256", sa.String(64), nullable=True))
        b.add_column(sa.Column("current_revision", sa.Integer(), nullable=True))
        b.add_column(sa.Column("reviewed_revision", sa.Integer(), nullable=True))
        b.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
        b.add_column(sa.Column("engine_type", sa.String(16), nullable=True))
        b.add_column(sa.Column("last_attempt_status", sa.Integer(), nullable=True))
        b.add_column(sa.Column("last_attempt_error", sa.String(500), nullable=True))
        b.create_unique_constraint("uq_document_info_sha256", ["file_sha256"])

    with op.batch_alter_table("document_extract") as b:
        b.add_column(sa.Column("field_key", sa.String(64), nullable=True))
        b.add_column(sa.Column("field_value_raw", sa.String(500), nullable=True))
        b.add_column(sa.Column("revision", sa.Integer(), nullable=True))

    with op.batch_alter_table("document_chunk") as b:
        b.add_column(sa.Column("revision", sa.Integer(), nullable=True))
        b.add_column(sa.Column("source_kind", sa.String(8), nullable=True,
                               server_default="text"))

    with op.batch_alter_table("doc_index_state") as b:
        b.add_column(sa.Column("indexed_revision", sa.Integer(), nullable=True))

    op.create_table(
        "document_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("doc_id", sa.Integer(),
                  sa.ForeignKey("document_info.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("change_reason", sa.String(32), nullable=False),
        sa.Column("doc_type", sa.String(16), nullable=False, server_default="other"),
        sa.Column("ocr_raw_text", sa.Text(), nullable=True),
        sa.Column("effective_text", sa.Text(), nullable=True),
        sa.Column("fields_json", sa.Text(), nullable=True),
        sa.Column("engine_type", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("doc_id", "revision", name="uq_document_revision_doc_rev"),
    )

    # ---------- 数据回填 ----------
    conn = op.get_bind()
    docs = conn.execute(sa.text(
        "SELECT id, ocr_raw_text, status FROM document_info"
    )).fetchall()

    ins_rev = sa.text(
        "INSERT INTO document_revision "
        "(doc_id, revision, change_reason, doc_type, ocr_raw_text, effective_text, "
        " fields_json, engine_type) "
        "VALUES (:doc_id, 1, 'legacy', 'other', :ocr, :eff, :fields, :engine)"
    )
    upd_doc = sa.text(
        "UPDATE document_info SET current_revision=:cur, engine_type=:engine, "
        "doc_type='other' WHERE id=:doc_id"
    )
    upd_ext = sa.text(
        "UPDATE document_extract SET field_key=:fkey, field_value_raw=field_value, "
        "revision=1 WHERE id=:ext_id"
    )

    for doc_id, ocr_text, status in docs:
        if status != 1:
            # 无有效结果的旧文档：不建快照，仅补类型默认值
            conn.execute(sa.text(
                "UPDATE document_info SET doc_type='other' WHERE id=:d"
            ), {"d": doc_id})
            continue

        engine = "mock" if _is_mock_text(ocr_text) else "legacy_unknown"
        ext_rows = conn.execute(sa.text(
            "SELECT id, field_name, field_value FROM document_extract WHERE doc_id=:d"
        ), {"d": doc_id}).fetchall()

        fields_snapshot = []
        for ext_id, fname, fval in ext_rows:
            fkey = _LEGACY_KEY_MAP.get(fname)
            fields_snapshot.append({
                "field_key": fkey, "field_name": fname,
                "field_value": fval, "field_value_raw": fval,
            })
            conn.execute(upd_ext, {"fkey": fkey, "ext_id": ext_id})

        conn.execute(ins_rev, {
            "doc_id": doc_id, "ocr": ocr_text, "eff": ocr_text,
            "fields": json.dumps(fields_snapshot, ensure_ascii=False),
            "engine": engine,
        })
        conn.execute(upd_doc, {"cur": 1, "engine": engine, "doc_id": doc_id})

    # 旧索引与新版本概念脱钩：清空索引状态，让启动补偿按 revision 重建
    conn.execute(sa.text("DELETE FROM document_chunk"))
    conn.execute(sa.text("DELETE FROM doc_index_state"))


def downgrade() -> None:
    op.drop_table("document_revision")
    with op.batch_alter_table("doc_index_state") as b:
        b.drop_column("indexed_revision")
    with op.batch_alter_table("document_chunk") as b:
        b.drop_column("source_kind")
        b.drop_column("revision")
    with op.batch_alter_table("document_extract") as b:
        b.drop_column("revision")
        b.drop_column("field_value_raw")
        b.drop_column("field_key")
    with op.batch_alter_table("document_info") as b:
        b.drop_constraint("uq_document_info_sha256", type_="unique")
        b.drop_column("last_attempt_error")
        b.drop_column("last_attempt_status")
        b.drop_column("engine_type")
        b.drop_column("deleted_at")
        b.drop_column("reviewed_revision")
        b.drop_column("current_revision")
        b.drop_column("file_sha256")
        b.drop_column("doc_type")
