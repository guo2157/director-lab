"""分镜图锁定保护与资产审核闸口冒烟测试（D-23 扩展）。

覆盖内容：
- 单镜 generate keyframe 时已锁定分镜图被拦截（409 asset_locked）
- 场景级批量 generate 时已锁定分镜图被拦截
- 项目级批量 generate 时已锁定分镜图被拦截
- 解锁后重新放行
- 公共资产未通过人工审核时三条通道均拦截（409 assets_review_pending）
"""
import json

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
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


IMAGE_WF = {
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


def _make_project(client, shots=3, approved=True):
    pid = client.post("/api/projects", json={"name": "锁定项目", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    ids = []
    for _ in range(shots):
        s = client.post("/api/shots", json={
            "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
        }).json()
        ids.append(s["id"])
    # D-23 资产就绪门禁：始终构造真实场景基准图资产（门禁先查资产完整性再查审核状态），
    # 审核状态按 approved 参数区分：False 时资产齐全但审核待定，命中 assets_review_pending
    from app.core.database import SessionLocal
    from app.models import Project, Scene
    from tests.conftest import approve_project_assets, seed_scene_baseline_asset
    _db = SessionLocal()
    try:
        _scene = _db.get(Scene, scn["id"])
        seed_scene_baseline_asset(_db, _scene, "/tmp/dlab_test_media")
        if approved:
            approve_project_assets(_db, pid)
    finally:
        _db.close()
    return {"pid": pid, "scene": scn, "shots": ids}


def _image_workflow(client, name):
    r = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": "image"},
        files={"file": (f"{name}.json", json.dumps(IMAGE_WF), "application/json")},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _gen_keyframe(client, shot_id, wf_id):
    j = client.post(f"/api/shots/{shot_id}/generate", json={"target": "keyframe", "workflow_id": wf_id}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True
    return jid


def _keyframe_asset_id(client, shot_id):
    detail = client.get(f"/api/shots/{shot_id}").json()
    return detail["current_assets"]["keyframe"]["asset_id"]


@pytest.fixture()
def mock_render(monkeypatch):
    import app.worker.executor as ex_mod
    from app.core.config import Settings
    monkeypatch.setattr(ex_mod, "get_settings", lambda: Settings(render_mock=True))


def test_single_shot_blocked_by_locked_keyframe(client, mock_render):
    """单镜生成：分镜图锁定后重新生成返回 409 asset_locked，解锁后放行。"""
    ctx = _make_project(client, shots=1)
    wf = _image_workflow(client, "lock-single")
    sid = ctx["shots"][0]
    _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, sid)
    assert client.post(f"/api/assets/{aid}/lock").status_code == 200
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "asset_locked"
    # 解锁后放行
    assert client.post(f"/api/assets/{aid}/unlock").status_code == 200
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf})
    assert r.status_code == 202, r.text


def test_scene_batch_blocked_by_locked_keyframe(client, mock_render):
    """场景级批量：任一镜头分镜图锁定即整体拦截。"""
    ctx = _make_project(client, shots=3)
    wf = _image_workflow(client, "lock-scene")
    for sid in ctx["shots"]:
        _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, ctx["shots"][1])
    assert client.post(f"/api/assets/{aid}/lock").status_code == 200
    r = client.post(f"/api/scenes/{ctx['scene']['id']}/generate", json={"target": "keyframe", "workflow_id": wf})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "asset_locked"
    # 解锁后不再因锁定拦截
    assert client.post(f"/api/assets/{aid}/unlock").status_code == 200
    r = client.post(f"/api/scenes/{ctx['scene']['id']}/generate", json={"target": "keyframe", "workflow_id": wf})
    assert r.status_code != 409 or r.json()["detail"]["code"] != "asset_locked", r.text


def test_project_batch_blocked_by_locked_keyframe(client, mock_render):
    """项目级批量：显式 shot_ids 时锁定同样拦截。"""
    ctx = _make_project(client, shots=3)
    wf = _image_workflow(client, "lock-project")
    for sid in ctx["shots"]:
        _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, ctx["shots"][0])
    assert client.post(f"/api/assets/{aid}/lock").status_code == 200
    r = client.post(
        f"/api/projects/{ctx['pid']}/generate",
        json={"shot_ids": ctx["shots"], "target": "keyframe", "workflow_id": wf},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "asset_locked"


def test_three_channels_blocked_without_review(client):
    """公共资产未审核通过时，三条出图通道均返回 409 assets_review_pending。"""
    ctx = _make_project(client, shots=3, approved=False)
    wf = _image_workflow(client, "review-image")
    sid = ctx["shots"][0]
    cases = [
        (f"/api/shots/{sid}/generate", {"target": "keyframe", "workflow_id": wf}),
        (f"/api/scenes/{ctx['scene']['id']}/generate", {"target": "keyframe", "workflow_id": wf}),
        (f"/api/projects/{ctx['pid']}/generate", {"shot_ids": ctx["shots"], "target": "keyframe", "workflow_id": wf}),
    ]
    for url, body in cases:
        r = client.post(url, json=body)
        assert r.status_code == 409, f"{url} -> {r.status_code} {r.text}"
        assert r.json()["detail"]["code"] == "assets_review_pending", f"{url} -> {r.text}"
