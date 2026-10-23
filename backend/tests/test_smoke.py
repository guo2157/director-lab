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
    assert body["status"] == "ok"
    assert body["app"] == "director-lab"


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
