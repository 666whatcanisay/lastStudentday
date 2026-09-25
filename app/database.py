"""
数据库连接与会话管理模块
使用 SQLite + SQLAlchemy ORM

schema 演进走 Alembic（方案 B 起）：
  - 新库            → alembic upgrade head
  - 旧库（无版本戳）→ 结构核对匹配旧基线后：一致性备份 → stamp 0001_legacy → upgrade head
  - 未知结构        → 拒绝启动并给出中文指引，**绝不删库重建**
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import BACKUP_DIR, BASE_DIR, DATABASE_PATH, DATABASE_URL

# 创建 SQLite 引擎（check_same_thread=False 允许 FastAPI 多线程访问）
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


# ==================== SQLite PRAGMA 配置 ====================
# 每条新连接建立后立即执行，用于修复以下三个默认行为带来的隐患：
#   1. foreign_keys  默认 OFF → DDL 里的 ondelete="CASCADE" 形同虚设，删除主表会留孤儿数据
#   2. journal_mode  默认 delete → 写入时全库加锁，并发写（后台建索引 + 用户删文档）会报 database is locked
#   3. busy_timeout  默认 0 → 遇到锁立即失败，不等待
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, conn_record):
    """为每条 SQLite 连接设置 PRAGMA（WAL 模式 + 外键约束 + 锁等待）"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")       # 启用外键约束（让 CASCADE 真正生效）
    cursor.execute("PRAGMA journal_mode=WAL")      # 写不阻塞读，提升并发能力
    cursor.execute("PRAGMA busy_timeout=5000")     # 锁冲突时等待 5s 而非立即失败
    cursor.execute("PRAGMA synchronous=NORMAL")    # WAL 模式下的推荐配置（性能与安全的平衡）
    cursor.close()

# 会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ORM 基类
Base = declarative_base()


def get_db():
    """获取数据库会话依赖（FastAPI Depends 用）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database():
    """初始化数据库：Alembic 驱动的建库/迁移（不再裸 create_all）"""
    from app import models  # noqa: F401 — 确保模型已注册
    _migrate_to_head()


# ==================== Alembic 迁移编排 ====================

LEGACY_REVISION = "0001_legacy"

# 旧基线（方案 B 之前 create_all 产物）的结构指纹：表名 → 列名集合
# 用于识别"该 stamp 旧基线"的存量库；只匹配完全一致的结构，不猜。
_LEGACY_FINGERPRINT = {
    "document_info": {"id", "file_name", "file_path", "file_type", "upload_time",
                      "ocr_raw_text", "infer_cost", "status", "error_msg"},
    "document_extract": {"id", "doc_id", "field_name", "field_value"},
    "document_chunk": {"id", "doc_id", "chunk_index", "chunk_text", "chunk_text_raw",
                       "char_start", "char_end", "page_no", "embedding", "embed_dim",
                       "embed_model", "created_at"},
    "doc_index_state": {"doc_id", "status", "chunk_count", "embed_model",
                        "error_msg", "updated_at"},
    "qa_record": {"id", "question", "rewritten_q", "answer", "hit_chunk_ids",
                  "top_score", "fusion_mode", "llm_model", "latency_ms", "created_at"},
}


def _alembic_cfg():
    from alembic.config import Config
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    return cfg


def _current_structure() -> dict:
    """当前库的实际结构：{表名: {列名集合}}"""
    insp = inspect(engine)
    return {t: {c["name"] for c in insp.get_columns(t)}
            for t in insp.get_table_names()}


def _detect_structure(actual: dict) -> str:
    """识别库结构：empty / legacy / current / unknown"""
    tables = set(actual) - {"alembic_version"}
    if not tables:
        return "empty"
    if "alembic_version" in actual:
        return "current"  # 有版本戳，交给 upgrade 判断
    # A single new column/table is not proof of a completed migration. Never
    # stamp head on a partially upgraded database, even if it resembles tests.
    if tables == set(_LEGACY_FINGERPRINT) and all(
        actual[t] == cols for t, cols in _LEGACY_FINGERPRINT.items()
    ):
        return "legacy"
    return "unknown"


def _consistency_backup() -> Path:
    """迁移前一致性备份（SQLite Online Backup API，WAL 下安全）"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"pre_migration_{ts}.db"
    src = sqlite3.connect(str(DATABASE_PATH))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _migrate_to_head():
    from alembic import command

    actual = _current_structure()
    kind = _detect_structure(actual)
    cfg = _alembic_cfg()

    if kind == "empty" or kind == "current":
        # 新库直接升到 head（迁移链自带旧基线 → 新结构）；有版本戳的库正常增量升级
        command.upgrade(cfg, "head")
        return

    if kind == "legacy":
        backup = _consistency_backup()
        command.stamp(cfg, LEGACY_REVISION)
        command.upgrade(cfg, "head")
        # 打印到 stdout 便于启动日志/脚本可见（不依赖 logging 配置）
        print(f"[database] 旧库已迁移，迁移前一致性备份：{backup}")
        return

    # unknown：绝不删库重建
    raise RuntimeError(
        "无法识别的数据库结构，已停止迁移以保护数据。\n"
        f"数据库文件：{DATABASE_PATH}\n"
        f"实际表结构：{sorted(actual)}\n"
        "处理方式：请先用 scripts/restore_backup.py 恢复可用备份，"
        "或将该文件移走后重新启动（系统会创建新库）。"
    )
