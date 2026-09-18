"""
数据库连接与会话管理模块
使用 SQLite + SQLAlchemy ORM
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

# 创建 SQLite 引擎（check_same_thread=False 允许 FastAPI 多线程访问）
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

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
