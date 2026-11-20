"""锁定功能与资产审核闸口已移除的回归测试（2026-11 流程简化）。

删除内容：
- 资产锁定（lock/unlock 端点、asset_locked 拒绝）
- 公共资产人工审核闸口（assets-review 接口、assets_review_pending 拒绝）

保留内容（必须继续可用）：
- 资产版本管理与引用保护（ref_count / validity）
- 分镜图重新生成 / 替换上传
- 生成任务状态管理
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


def _make_project(client, shots=3):
    pid = client.post("/api/projects", json={"name": "锁定项目", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    ids = []
    for _ in range(shots):
        s = client.post("/api/shots", json={
            "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
        }).json()
        ids.append(s["id"])
    # 资产就绪门禁：构造真实场景基准图资产（门禁校验指针/有效性/文件存在）
    from app.core.database import SessionLocal
    from app.models import Scene
    from tests.conftest import seed_scene_baseline_asset
    _db = SessionLocal()
    try:
        _scene = _db.get(Scene, scn["id"])
        seed_scene_baseline_asset(_db, _scene, "/tmp/dlab_test_media")
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


# --- 锁定功能已移除 ---


def test_lock_endpoints_removed(client, mock_render):
    """lock/unlock 端点已删除：单条与批量通道都不再存在。"""
    ctx = _make_project(client, shots=1)
    wf = _image_workflow(client, "lock-single")
    sid = ctx["shots"][0]
    _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, sid)
    for url in (
        f"/api/assets/{aid}/lock",
        f"/api/assets/{aid}/unlock",
        "/api/assets/batch/lock",
        "/api/assets/batch/unlock",
    ):
        r = client.post(url, json={"ids": [aid]})
        assert r.status_code in (404, 405), f"{url} -> {r.status_code} {r.text}"


def test_locked_field_always_false(client, mock_render):
    """资产输出保留 locked 字段兼容旧调用方，但恒为 False（无写入通道）。"""
    ctx = _make_project(client, shots=1)
    wf = _image_workflow(client, "lock-field")
    sid = ctx["shots"][0]
    _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, sid)
    body = client.get(f"/api/assets/{aid}").json()
    assert body["locked"] is False


def test_regenerate_not_blocked_by_locked_flag(client, mock_render):
    """历史数据里 locked=True 的资产也不再阻止重新生成（锁定不再是业务限制）。"""
    ctx = _make_project(client, shots=1)
    wf = _image_workflow(client, "lock-regen")
    sid = ctx["shots"][0]
    _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, sid)

    from app.core.database import SessionLocal
    from app.models import Asset
    _db = SessionLocal()
    try:
        a = _db.get(Asset, aid)
        a.locked = True  # 模拟历史遗留的锁定数据
        _db.commit()
    finally:
        _db.close()

    # 单镜重新生成：不再返回 asset_locked
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf})
    assert r.status_code == 202, r.text
    # 替换上传：不再返回 asset_locked
    r = client.post(
        f"/api/assets/{aid}/replace",
        files={"file": ("new.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
    )
    assert r.status_code == 200, r.text


def test_delete_not_blocked_by_locked_flag(client, mock_render):
    """删除资产只受引用保护（ref_count）约束，锁定不再是阻断条件。"""
    ctx = _make_project(client, shots=1)
    wf = _image_workflow(client, "lock-del")
    sid = ctx["shots"][0]
    _gen_keyframe(client, sid, wf)
    aid = _keyframe_asset_id(client, sid)

    from app.core.database import SessionLocal
    from app.models import Asset, AssetRef
    _db = SessionLocal()
    try:
        # 清掉引用关系，构造「无引用 + 已锁定」的历史资产
        _db.query(AssetRef).filter(AssetRef.asset_id == aid).delete()
        a = _db.get(Asset, aid)
        a.locked = True
        a.ref_count = 0
        _db.commit()
    finally:
        _db.close()
    r = client.delete(f"/api/assets/{aid}")
    assert r.status_code == 204, r.text


# --- 资产审核闸口已移除 ---


def test_assets_review_endpoint_removed(client):
    """资产人工审核接口已删除（404/405），不再有审核状态写入通道。"""
    ctx = _make_project(client, shots=1)
    for action in ("approve", "reject"):
        r = client.post(f"/api/projects/{ctx['pid']}/assets-review", json={"action": action})
        assert r.status_code in (404, 405), r.text


def test_keyframe_generation_no_longer_requires_review(client, mock_render):
    """资产齐全后三条出图通道都放行，不再返回 assets_review_pending。

    三条通道各自建独立项目：同一项目上连续提交会命中在途任务去重（job_in_flight），
    那是另一个闸口，与本用例验证的资产审核无关。
    """
    channels = ("single", "scene", "project")
    for ch in channels:
        ctx = _make_project(client, shots=3)
        wf = _image_workflow(client, f"review-image-{ch}")
        sid = ctx["shots"][0]
        if ch == "single":
            url, body = f"/api/shots/{sid}/generate", {"target": "keyframe", "workflow_id": wf}
        elif ch == "scene":
            url = f"/api/scenes/{ctx['scene']['id']}/generate"
            body = {"target": "keyframe", "workflow_id": wf}
        else:
            url = f"/api/projects/{ctx['pid']}/generate"
            body = {"shot_ids": ctx["shots"], "target": "keyframe", "workflow_id": wf}
        r = client.post(url, json=body)
        assert r.status_code == 202, f"{ch} {url} -> {r.status_code} {r.text}"
        assert "assets_review" not in (r.text or ""), f"{ch} -> {r.text}"
