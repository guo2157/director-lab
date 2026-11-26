"""Agent Runtime Phase A 迁移测试：新增列可升降级。

alembic/env.py 会用 get_settings().database_url 覆盖 Config 里的 sqlalchemy.url，
所以只设置 Config 无效：这里通过 DLAB_DATABASE_URL 环境变量 + 清空 Settings 缓存，
把迁移真正指向 tmp 库，并在迁移前后断言解析出的地址就是 tmp 库。
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
# A0（a1b2c3d4e5f9）新增列
PHASE_A_COLUMNS = {
    "agent_plan_step": {"confirmation_id", "job_id", "step_key"},
    "agent_goal": {"fingerprint"},
    "agent_plan": {"parent_plan_id"},
}
# A11（a1b2c3d4e5fa）新增锁字段
A11_COLUMNS = {
    "agent_plan_step": {"lock_owner"},
    "agent_goal": {"locked_until"},
}
NEW_COLUMNS = {
    table: PHASE_A_COLUMNS.get(table, set()) | A11_COLUMNS.get(table, set())
    for table in set(PHASE_A_COLUMNS) | set(A11_COLUMNS)
}
PHASE_A_HEAD = "a1b2c3d4e5f9"
A11_HEAD = "a1b2c3d4e5fa"


def _config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _columns(db_url: str) -> dict[str, set[str]]:
    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        return {table: {c["name"] for c in inspector.get_columns(table)} for table in NEW_COLUMNS}
    finally:
        engine.dispose()


def _alembic_version(db_url: str) -> str:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("select version_num from alembic_version")).scalar_one()
    finally:
        engine.dispose()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = (tmp_path / "migration.db").resolve()
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DLAB_DATABASE_URL", db_url)

    from app.core.config import clear_settings_cache, get_settings

    clear_settings_cache()
    try:
        resolved = Path(make_url(get_settings().database_url).database).resolve()
        assert resolved == db_path, f"迁移测试的 Settings 未指向 tmp 库：{resolved}"
        yield db_url, db_path
    finally:
        # 用例结束后恢复 conftest 设定的临时库地址，避免污染后续用例
        clear_settings_cache()


def test_phase_a_columns_upgrade_and_downgrade(tmp_db):
    db_url, db_path = tmp_db
    cfg = _config(db_url)

    command.upgrade(cfg, A11_HEAD)
    assert db_path.exists(), "upgrade 后 tmp 库文件不存在，说明迁移没有连到 tmp 库"
    cols = _columns(db_url)
    for table, expected in NEW_COLUMNS.items():
        assert expected <= cols[table], f"{table} missing {expected - cols[table]}"
    assert _alembic_version(db_url) == A11_HEAD

    # 先回退 A11（锁字段），再回退 A0（Phase A 列）
    command.downgrade(cfg, PHASE_A_HEAD)
    cols = _columns(db_url)
    for table, expected in A11_COLUMNS.items():
        assert not (expected & cols[table]), f"{table} still has {expected & cols[table]}"
    for table, expected in PHASE_A_COLUMNS.items():
        assert expected <= cols[table], f"{table} 回退 A11 后不应丢失 A0 列"

    command.downgrade(cfg, "-1")
    cols = _columns(db_url)
    for table, expected in NEW_COLUMNS.items():
        assert not (expected & cols[table]), f"{table} still has {expected & cols[table]}"
    assert _alembic_version(db_url) == "g1a2b3c4d5e7"

    command.upgrade(cfg, A11_HEAD)
    cols = _columns(db_url)
    for table, expected in NEW_COLUMNS.items():
        assert expected <= cols[table]


def test_settings_url_follows_env(tmp_db):
    """确认 alembic/env.py 读取的地址就是 tmp 库（隔离机制本身的自测）。"""
    db_url, db_path = tmp_db
    from app.core.config import clear_settings_cache, get_settings

    clear_settings_cache()
    assert Path(make_url(get_settings().database_url).database).resolve() == db_path
