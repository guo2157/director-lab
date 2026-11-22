"""阶段 1 冒烟测试：基础设施验收。"""
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import make_engine
from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("healthy", "degraded", "down")
    assert "database" in body
    assert "comfyui" in body


def test_health_worker_status_reflects_heartbeat(tmp_path, monkeypatch):
    """worker 状态必须来自心跳文件，不能硬编码。

    历史事故：worker 未启动时任务长期停在 queued，而 /api/health 仍报
    worker="ok"，用户与前端都看不出后端没在消费任务。
    """
    import time as _time
    from app.worker import scheduler

    hb = tmp_path / "worker_heartbeat"
    monkeypatch.setattr(scheduler, "heartbeat_path", lambda: hb)

    # 1) 无心跳文件 → not_running
    assert client.get("/api/health").json()["worker"] == "not_running"

    # 2) 新鲜心跳 → ok
    hb.write_text(str(_time.time()), encoding="utf-8")
    body = client.get("/api/health").json()
    assert body["worker"] == "ok"
    assert body["worker_last_beat"] is not None

    # 3) 过期心跳 → stale，且整体降级
    hb.write_text(str(_time.time() - 3600), encoding="utf-8")
    body = client.get("/api/health").json()
    assert body["worker"] == "stale"
    assert body["status"] == "degraded"


def test_404_uses_unified_error_format():
    resp = client.get("/api/nonexistent-route")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "code" in detail
    assert "message" in detail


def test_runtime_engine_enables_foreign_keys(tmp_path):
    db_file = tmp_path / "t.db"
    engine = make_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert fk == 1
    assert mode == "wal"
