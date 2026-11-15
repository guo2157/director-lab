"""数据库引擎与会话工厂。

纪律（AGENTS.md §3）：外键约束必须开启；SQLite WAL 模式由迁移环境初始化，
运行时每个连接同样设置，保证读写一致。
"""
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    # API 与 worker 并发写库时，默认 5s 的锁等待会直接抛 database is locked
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def make_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    event.listen(engine, "connect", _set_sqlite_pragma)
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
