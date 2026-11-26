"""Phase B B0 验收：迁移列可升降级 + 事件白名单新增类型可用。"""
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREV_HEAD = "a1b2c3d4e5fa"
HEAD = "a1b2c3d4e5fb"
NEW_COLUMNS = {
    "agent_plan_step": {"resume_requested_at", "resume_reason"},
    "agent_goal": {"agent_task_id", "task_id", "lifecycle_state", "last_tick_at"},
}


def _config(db_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _columns(db_url: str) -> dict[str, set[str]]:
    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        return {t: {c["name"] for c in inspector.get_columns(t)} for t in NEW_COLUMNS}
    finally:
        engine.dispose()


def _version(db_url: str) -> str:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("select version_num from alembic_version")).scalar_one()
    finally:
        engine.dispose()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = (tmp_path / "phase_b.db").resolve()
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DLAB_DATABASE_URL", db_url)
    from app.core.config import clear_settings_cache, get_settings

    clear_settings_cache()
    try:
        resolved = Path(make_url(get_settings().database_url).database).resolve()
        assert resolved == db_path, f"迁移测试未指向 tmp 库：{resolved}"
        yield db_url, db_path
    finally:
        clear_settings_cache()


def test_phase_b_columns_upgrade_and_downgrade(tmp_db):
    db_url, db_path = tmp_db
    cfg = _config(db_url)

    command.upgrade(cfg, "head")
    assert db_path.exists(), "upgrade 未连到 tmp 库"
    cols = _columns(db_url)
    for table, expected in NEW_COLUMNS.items():
        assert expected <= cols[table], f"{table} missing {expected - cols[table]}"
    assert _version(db_url) == HEAD

    command.downgrade(cfg, PREV_HEAD)
    cols = _columns(db_url)
    for table, expected in NEW_COLUMNS.items():
        assert not (expected & cols[table]), f"{table} still has {expected & cols[table]}"
    assert _version(db_url) == PREV_HEAD

    # 回退后旧列仍在（只加不删），再次升级可重复
    command.upgrade(cfg, "head")
    cols = _columns(db_url)
    for table, expected in NEW_COLUMNS.items():
        assert expected <= cols[table]
    assert {"lock_owner"} <= cols["agent_plan_step"]


def test_phase_b_columns_are_nullable(tmp_db):
    """B0 约束：新增列必须全部可空（旧代码在未升级库上也能运行，新列不影响旧写入）。"""
    db_url, _ = tmp_db
    command.upgrade(_config(db_url), "head")
    engine = create_engine(db_url)
    try:
        inspector = inspect(engine)
        for table, expected in NEW_COLUMNS.items():
            by_name = {c["name"]: c for c in inspector.get_columns(table)}
            for col in expected:
                assert by_name[col]["nullable"] is True, f"{table}.{col} 必须可空"
    finally:
        engine.dispose()


def test_agent_event_types_registered(db):
    from app.services.workspace_event import EVENT_TYPES, write_workspace_event

    expected = {
        "agent.goal.created",
        "agent.goal.updated",
        "agent.goal.superseded",
        "agent.goal.finished",
        "agent.plan.created",
        "agent.step.updated",
        "agent.tick.finished",
    }
    assert expected <= EVENT_TYPES

    ev = write_workspace_event(
        db,
        "agent.goal.created",
        session_id="ags_b0",
        project_id="prj_b0",
        payload={"goal_id": "gol_b0", "status": "active"},
    )
    db.commit()
    assert ev.event_type == "agent.goal.created"
    assert ev.payload["goal_id"] == "gol_b0"


def test_unknown_event_type_still_rejected(db):
    from app.services.workspace_event import write_workspace_event

    with pytest.raises(ValueError):
        write_workspace_event(db, "agent.unknown.event", payload={})


def test_agent_event_names_fit_column(db):
    """event_type 列是 String(48)，名字不得超长。"""
    from app.services.workspace_event import EVENT_TYPES

    too_long = [t for t in EVENT_TYPES if len(t) > 48]
    assert not too_long, f"事件名超出列宽：{too_long}"