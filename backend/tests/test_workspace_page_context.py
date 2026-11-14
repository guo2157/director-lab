"""工作台页面上下文测试（导演工作台 V2 Phase 4）。

覆盖：
- 首次上报：单行建表、revision 从 0 递增到 1
- 递增上报：不带 expected_revision 时正常推进
- 冲突检测：expected_revision 不一致返回 409，且响应内含服务端当前快照
- force 覆盖：显式要求时忽略版本不一致
- 非法实体类型：422 拒绝
- 未建模字段：进入 extra，不丢信息
- 事件：上报成功同事务落 workspace.page_context 事件
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base
from app.models.workspace_event import WorkspaceEvent
from app.models.workspace_page_context import WorkspacePageContext


@pytest.fixture()
def client(tmp_path, monkeypatch):
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
    import app.core.database as db_mod

    s = db_mod.SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_first_report_starts_at_revision_one(client: TestClient, db):
    """首次上报：建单行记录，revision 从 0 → 1。"""
    r = client.post(
        "/api/workspace/page-context",
        json={
            "page": "assets",
            "tab": "character",
            "entity_type": "character",
            "entity_id": "chr_1",
            "nav_ok": True,
            "nav_result": "nav_ok",
            "route_name": "assets",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["context"]["page"] == "assets"
    assert body["context"]["entity_id"] == "chr_1"

    rows = db.query(WorkspacePageContext).all()
    assert len(rows) == 1


def test_read_before_any_report(client: TestClient):
    """未上报时读取：revision=0、context=null，不报错。"""
    r = client.get("/api/workspace/page-context")
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 0
    assert r.json()["context"] is None


def test_revision_increments_on_repeat(client: TestClient):
    """连续上报：revision 单调递增，且仍只有一行记录。"""
    for page in ("assets", "storyboard", "jobs"):
        r = client.post("/api/workspace/page-context", json={"page": page})
        assert r.status_code == 200, r.text
    assert r.json()["revision"] == 3
    assert r.json()["context"]["page"] == "jobs"


def test_revision_conflict_returns_409_with_snapshot(client: TestClient):
    """expected_revision 不一致：409，且返回服务端当前快照供前端刷新。"""
    client.post("/api/workspace/page-context", json={"page": "assets"})
    client.post("/api/workspace/page-context", json={"page": "storyboard"})

    r = client.post(
        "/api/workspace/page-context",
        json={"page": "script", "expected_revision": 0},
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "context_revision_conflict"
    assert detail["current_revision"] == 2
    assert detail["expected_revision"] == 0
    # 冲突时服务端不得被改写
    assert detail["current"]["page"] == "storyboard"

    r = client.get("/api/workspace/page-context")
    assert r.json()["context"]["page"] == "storyboard"
    assert r.json()["revision"] == 2


def test_matching_expected_revision_succeeds(client: TestClient):
    """expected_revision 与当前一致：允许写入。"""
    first = client.post("/api/workspace/page-context", json={"page": "assets"}).json()
    rev = first["revision"]
    r = client.post(
        "/api/workspace/page-context",
        json={"page": "compose", "expected_revision": rev},
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == rev + 1
    assert r.json()["context"]["page"] == "compose"


def test_force_overrides_conflict(client: TestClient):
    """force=true：显式强制覆盖，不因版本不一致失败。"""
    client.post("/api/workspace/page-context", json={"page": "assets"})
    r = client.post(
        "/api/workspace/page-context",
        json={"page": "settings", "expected_revision": 0, "force": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["context"]["page"] == "settings"


def test_invalid_entity_type_rejected(client: TestClient):
    """未登记的实体类型：422 拒绝，不写入脏数据。"""
    r = client.post(
        "/api/workspace/page-context",
        json={"page": "assets", "entity_type": "spaceship"},
    )
    assert r.status_code == 422, r.text
    assert client.get("/api/workspace/page-context").json()["revision"] == 0


def test_unknown_fields_go_to_extra(client: TestClient):
    """未建模字段进 extra：前端新增字段无需改后端表结构。"""
    r = client.post(
        "/api/workspace/page-context",
        json={"page": "assets", "scroll_top": 120, "panel": "left"},
    )
    assert r.status_code == 200, r.text
    extra = r.json()["context"]["extra"] or {}
    assert extra.get("scroll_top") == 120
    assert extra.get("panel") == "left"


def test_report_writes_page_context_event(client: TestClient, db):
    """上报成功：同事务落 workspace.page_context 事件，含 page/revision。"""
    r = client.post(
        "/api/workspace/page-context",
        json={
            "page": "storyboard",
            "route_name": "storyboard",
            "entity_type": "shot",
            "entity_id": "sht_9",
        },
    )
    assert r.status_code == 200, r.text

    events = (
        db.query(WorkspaceEvent)
        .filter(WorkspaceEvent.event_type == "workspace.page_context")
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.route_name == "storyboard"
    assert (ev.payload or {}).get("page") == "storyboard"
    assert (ev.payload or {}).get("entity_id") == "sht_9"
    assert (ev.payload or {}).get("revision") == 1


def test_conflict_does_not_write_event(client: TestClient, db):
    """冲突（409）不得留下事件：失败操作不能污染事件流。"""
    client.post("/api/workspace/page-context", json={"page": "assets"})
    before = db.query(WorkspaceEvent).count()

    r = client.post(
        "/api/workspace/page-context",
        json={"page": "script", "expected_revision": 0},
    )
    assert r.status_code == 409

    after = db.query(WorkspaceEvent).count()
    assert after == before
