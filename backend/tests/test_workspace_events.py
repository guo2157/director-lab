"""工作台事件 outbox 测试（导演工作台 V2 Phase 3）。

覆盖：
- 事件类型白名单：未登记类型必须拒绝（防止事件语义漂移）
- 增量查询：seq 严格大于语义、升序返回、session 过滤
- 序列化：payload 不得覆盖 seq/event_type 等信封字段
- 导航：chat 产生 navigate_workspace 时同步落 workspace.navigate 事件
- 接口：GET /api/workspace/events/backfill 真实返回
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base
from app.models.workspace_event import WorkspaceEvent
from app.services.workspace_event import (
    EVENT_TYPES,
    fetch_events_after,
    serialize_event,
    write_workspace_event,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """独立 SQLite + 打补丁的引擎/会话工厂，与既有 api 测试风格一致。"""
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def db(client):
    """与被打补丁的引擎共享同一数据库文件的会话。"""
    import app.core.database as db_mod

    s = db_mod.SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_unknown_event_type_rejected(db):
    """未登记的事件类型必须直接拒绝，不允许自由写入。"""
    with pytest.raises(ValueError):
        write_workspace_event(db, "workspace.not_registered", session_id="ags_x")


def test_write_and_incremental_fetch(db):
    """写入两条事件后按 seq 增量读取，语义为严格大于且升序。"""
    first = write_workspace_event(
        db,
        "workspace.navigate",
        session_id="ags_a",
        route_name="script",
        payload={"route_name": "script"},
    )
    db.commit()
    second = write_workspace_event(
        db,
        "workspace.approval_resolved",
        session_id="ags_a",
        payload={"confirmation_id": "cfm_1"},
    )
    db.commit()

    assert second.event_seq > first.event_seq

    got = fetch_events_after(db, first.event_seq)
    assert [e.event_seq for e in got] == [second.event_seq]

    all_events = fetch_events_after(db, 0)
    assert [e.event_seq for e in all_events] == [first.event_seq, second.event_seq]


def test_fetch_filters_by_session(db):
    """按 session_id 过滤：只返回该会话事件。"""
    write_workspace_event(db, "workspace.navigate", session_id="ags_one", route_name="assets")
    write_workspace_event(db, "workspace.navigate", session_id="ags_two", route_name="jobs")
    db.commit()

    rows = fetch_events_after(db, 0, session_id="ags_two")
    assert len(rows) == 1
    assert rows[0].route_name == "jobs"


def test_serialize_keeps_envelope_fields(db):
    """payload 中的同名键不得覆盖 seq/event_type 等信封字段。"""
    ev = write_workspace_event(
        db,
        "workspace.navigate",
        session_id="ags_x",
        payload={"seq": 9999, "event_type": "fake.type", "route_action": "navigate_workspace"},
    )
    db.commit()

    data = serialize_event(ev)
    assert data["seq"] == ev.event_seq
    assert data["seq"] != 9999
    assert data["event_type"] == "workspace.navigate"
    assert data["route_action"] == "navigate_workspace"


def test_event_types_whitelist_shape():
    """白名单覆盖工作台阶段事实，且无重复。"""
    assert len(EVENT_TYPES) == len(set(EVENT_TYPES))
    assert "workspace.navigate" in EVENT_TYPES
    assert "workspace.approval_resolved" in EVENT_TYPES


def test_navigation_chat_writes_event(client: TestClient, db):
    """导航请求：assistant payload 带 navigate_workspace，同时落 workspace.navigate 事件。"""
    r = client.post("/api/agent/sessions", json={"title": "导航事件测试"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "去分镜页"})
    assert r.status_code == 200, r.text
    payload = r.json()["assistant_message"]["payload"]
    assert payload.get("route_action") == "navigate_workspace"
    assert payload.get("route_name") == "storyboard"

    events = db.query(WorkspaceEvent).filter(WorkspaceEvent.session_id == sid).all()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "workspace.navigate"
    assert ev.route_name == "storyboard"
    assert (ev.payload or {}).get("route_name") == "storyboard"


def test_backfill_endpoint_returns_events(client: TestClient, db):
    """接口层：backfill 按 updated_since_seq 返回增量事件，语义严格大于。"""
    ev = write_workspace_event(db, "workspace.navigate", session_id="ags_api", route_name="settings")
    db.commit()

    r = client.get("/api/workspace/events/backfill", params={"updated_since_seq": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    assert ev.event_seq in [i["seq"] for i in body["items"]]

    r = client.get("/api/workspace/events/backfill", params={"updated_since_seq": ev.event_seq})
    assert ev.event_seq not in [i["seq"] for i in r.json()["items"]]


def test_backfill_endpoint_filters_session(client: TestClient, db):
    """接口层：session_id 过滤生效。"""
    write_workspace_event(db, "workspace.navigate", session_id="ags_keep", route_name="jobs")
    write_workspace_event(db, "workspace.navigate", session_id="ags_drop", route_name="compose")
    db.commit()

    r = client.get(
        "/api/workspace/events/backfill",
        params={"updated_since_seq": 0, "session_id": "ags_keep"},
    )
    assert r.status_code == 200, r.text
    assert all(i["session_id"] == "ags_keep" for i in r.json()["items"])
