"""Alembic 迁移环境：数据库地址统一取自 app.core.config，禁止在此单独维护。"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

config = context.config
if config.config_file_name is not None:
    # 迁移在应用进程内执行，fileConfig 默认会禁用已存在的 logger，
    # 导致 uvicorn 的访问日志与异常堆栈从此静默；这里保留现有 logger
    fileConfig(config.config_file_name, disable_existing_loggers=False)

from app.core.config import get_settings  # noqa: E402
from app.models.base import Base  # noqa: E402

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def _set_sqlite_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    event.listen(connectable, "connect", _set_sqlite_pragma)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
