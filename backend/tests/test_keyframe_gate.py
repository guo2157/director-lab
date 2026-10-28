"""第二期测试（D-13 静图先行闸口 / D-14 批量重试）。

闸口规则：
- 生成 clip 前校验分镜图已人工确认，未确认返回 409 keyframe_not_confirmed
- 确认前须存在当前有效分镜图产物，否则 409 keyframe_not_ready
- 重做分镜图（regenerate keyframe）后确认自动失效
- 批量重试：failed/lost 且未达上限的任务创建新任务，已达上限的跳过
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import make_engine
from app.models import Base


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def _make_shot(client: TestClient) -> dict:
    pid = client.post("/api/projects", json={"name": "闸口项目", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    # D-23 资产审核闸口：本文件聚焦分镜图确认闸口（D-13），
    # 直接放行公共资产人工审核，避免与资产审核前置条件耦合
    from app.core.database import SessionLocal
    from app.models import Project
    _db = SessionLocal()
    try:
        _project = _db.get(Project, pid)
        _project.assets_review_status = "approved"
        _db.commit()
    finally:
        _db.close()
    return {"project_id": pid, "scene": scn, "shot": shot}


def test_clip_blocked_without_confirmation(client: TestClient):
    """未确认分镜图时生成 clip 返回 409 keyframe_not_confirmed。"""
    ctx = _make_shot(client)
    r = client.post(f"/api/shots/{ctx['shot']['id']}/generate", json={"target": "clip"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "keyframe_not_confirmed"


def test_confirm_requires_keyframe_asset(client: TestClient):
    """分镜图未生成时确认返回 409 keyframe_not_ready。"""
    ctx = _make_shot(client)
    r = client.post(f"/api/shots/{ctx['shot']['id']}/keyframe/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "keyframe_not_ready"


def test_confirm_then_clip_passes_gate(client: TestClient, monkeypatch):
    """生成 mock 分镜图 → 确认 → clip 生成放行（闸口通过）。"""
    import app.core.config as cfg_mod
    import app.worker.executor as ex_mod
    from app.core.config import Settings
    monkeypatch.setattr(ex_mod, "get_settings", lambda: Settings(render_mock=True))

    ctx = _make_shot(client)
    sid = ctx["shot"]["id"]
    # 1. 生成 mock 分镜图
    import json
    definition = {
        "1": {
            "class_type": "EmptyLatentImage",
            "_meta": {"title": "$~width, $~height"},
            "inputs": {"width": 512, "height": 288, "batch_size": 1},
        },
        "2": {
            "class_type": "SaveImage",
            "_meta": {"title": "$output.cover"},
            "inputs": {"images": ["1", 0]},
        },
    }
    wf = client.post(
        "/api/workflows",
        data={"name": "gate-image", "workflow_type": "image"},
        files={"file": ("gate_image.json", json.dumps(definition), "application/json")},
    )
    assert wf.status_code == 201, wf.text
    j = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf.json()["id"]}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True
    # 2. 确认分镜图
    r = client.post(f"/api/shots/{sid}/keyframe/confirm")
    assert r.status_code == 200 and r.json()["keyframe_confirmed"] is True
    # 3. clip 生成放行（闸口通过，任务创建成功）
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "clip", "workflow_id": wf.json()["id"]})
    assert r.status_code == 202, r.text


def test_regenerate_keyframe_resets_confirmation(client: TestClient, monkeypatch):
    """重做分镜图后确认自动失效，clip 生成重新被闸口拦截。"""
    import app.core.config as cfg_mod
    import app.worker.executor as ex_mod
    from app.core.config import Settings
    monkeypatch.setattr(ex_mod, "get_settings", lambda: Settings(render_mock=True))

    ctx = _make_shot(client)
    sid = ctx["shot"]["id"]
    import json
    definition = {
        "1": {
            "class_type": "EmptyLatentImage",
            "_meta": {"title": "$~width, $~height"},
            "inputs": {"width": 512, "height": 288, "batch_size": 1},
        },
        "2": {
            "class_type": "SaveImage",
            "_meta": {"title": "$output.cover"},
            "inputs": {"images": ["1", 0]},
        },
    }
    wf = client.post(
        "/api/workflows",
        data={"name": "gate-image2", "workflow_type": "image"},
        files={"file": ("gate_image2.json", json.dumps(definition), "application/json")},
    ).json()
    j = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf["id"]}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True
    assert client.post(f"/api/shots/{sid}/keyframe/confirm").status_code == 200
    # 重做分镜图 → 确认失效
    r = client.post(f"/api/shots/{sid}/regenerate", json={"target": "keyframe", "workflow_id": wf["id"]})
    assert r.status_code == 202
    detail = client.get(f"/api/shots/{sid}").json()
    assert detail.get("keyframe_confirmed") is False
    # clip 生成重新被拦截
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "clip", "workflow_id": wf["id"]})
    assert r.status_code == 409


def test_batch_retry_failed_jobs(client: TestClient):
    """批量重试：failed 任务创建新任务，已达上限的跳过。"""
    from datetime import datetime, timezone
    from app.core.database import SessionLocal
    from app.models import Job, ids

    ctx = _make_shot(client)
    pid = ctx["project_id"]
    db = SessionLocal()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    j1 = Job(id=ids.job_id(), project_id=pid, job_type="generate_keyframe",
             status="failed", attempts=1, created_at=now)
    j2 = Job(id=ids.job_id(), project_id=pid, job_type="generate_clip",
             status="failed", attempts=3, created_at=now)  # 默认上限 3，不可重试
    db.add_all([j1, j2])
    db.commit()
    db.close()

    r = client.post(f"/api/jobs/projects/{pid}/retry-failed", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["retried_count"] == 1
    assert body["skipped_count"] == 1
    assert body["retried"][0]["source_job_id"] == j1.id
    # 新任务已入队
    new_id = body["retried"][0]["new_job_id"]
    lst = client.get(f"/api/jobs?project_id={pid}&status=queued").json()
    assert new_id in [i["id"] for i in lst["items"]]
