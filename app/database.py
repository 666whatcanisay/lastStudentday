"""
数据库连接与会话管理模块
使用 SQLite + SQLAlchemy ORM
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

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
    """初始化数据库：创建所有表（仅创建不存在的表，不删除已有数据）"""
    from app import models  # noqa: F401 — 确保模型已注册
    Base.metadata.create_all(bind=engine)
