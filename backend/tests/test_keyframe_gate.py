"""分镜图就绪校验与批量重试测试（2026-11 流程简化后）。

闸口规则变更：
- 旧：生成 clip 前校验 keyframe_confirmed（人工逐镜确认），未确认返回 409 keyframe_not_confirmed
- 新：生成 clip 前校验该镜有**当前有效且文件存在**的分镜图，缺失返回 409 keyframe_not_ready
- 逐镜人工确认接口 /keyframe/confirm、/keyframe/unconfirm 与批量确认已全部移除；
  数据库字段 keyframe_confirmed 保留（兼容历史数据），但不再参与任何门禁判定

覆盖：
1. 没有分镜图时生成 clip → 409 keyframe_not_ready
2. 生成分镜图后生成 clip → 202（无需确认）
3. 分镜图文件被删除 → 生成 clip 被拦截
4. 分镜图版本转 superseded（重绘/替换）→ 生成 clip 被拦截
5. 批量重试：failed/lost 且未达上限的任务创建新任务，已达上限的跳过
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
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


_IMAGE_WF = {
    "1": {
        "class_type": "EmptyLatentImage",
        "_meta": {"title": "$width, $height"},
        "inputs": {"width": 512, "height": 288, "batch_size": 1},
    },
    "2": {
        "class_type": "SaveImage",
        "_meta": {"title": "$output.cover"},
        "inputs": {"images": ["1", 0]},
    },
}

_VIDEO_WF = {
    "1": {
        "class_type": "EmptyLatentImage",
        "_meta": {"title": "$width, $height"},
        "inputs": {"width": 512, "height": 288, "batch_size": 1},
    },
    "2": {
        "class_type": "SaveVideo",
        "_meta": {"title": "$output.video"},
        "inputs": {"images": ["1", 0]},
    },
}


def _make_shot(client: TestClient) -> dict:
    pid = client.post("/api/projects", json={"name": "闸口项目", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    # 资产就绪门禁：构造真实场景基准图资产，避免与资产就绪前置条件耦合
    from app.core.database import SessionLocal
    from app.models import Scene
    from tests.conftest import seed_scene_baseline_asset
    _db = SessionLocal()
    try:
        _scene = _db.get(Scene, scn["id"])
        seed_scene_baseline_asset(_db, _scene, "/tmp/dlab_test_media")
    finally:
        _db.close()
    return {"project_id": pid, "scene": scn, "shot": shot}


def _workflow(client: TestClient, name: str, wf_type: str, definition: dict) -> str:
    r = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": wf_type},
        files={"file": (f"{name}.json", json.dumps(definition), "application/json")},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture()
def mock_render(monkeypatch, tmp_path):
    """mock 渲染必须落在临时 media_dir，禁止写真实 backend/data/media。"""
    import app.worker.executor as ex_mod
    from app.core.config import Settings
    monkeypatch.setattr(
        ex_mod, "get_settings", lambda: Settings(render_mock=True, media_dir=tmp_path / "media")
    )


def test_clip_blocked_without_keyframe(client: TestClient, mock_render):
    """没有分镜图时生成 clip 返回 409 keyframe_not_ready。"""
    ctx = _make_shot(client)
    r = client.post(f"/api/shots/{ctx['shot']['id']}/generate", json={"target": "clip"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "keyframe_not_ready"


def test_confirm_endpoint_removed(client: TestClient):
    """逐镜人工确认接口已随机制取消一并移除，不再对外提供。

    注意：返回码取决于前端产物是否已构建——
    - 无 dist（SPA 未挂载）→ 404 Not Found；
    - 有 dist（SPA 兜底路由 GET /{full_path:path} 已挂载）→ 405 Method Not Allowed。
    两者都表示「业务端点不存在」。断言必须同时接受，否则会在
    `npm run build` 前后出现假失败（构建产物是 gitignore 的，CI 与本地不一致）。
    """
    ctx = _make_shot(client)
    r = client.post(f"/api/shots/{ctx['shot']['id']}/keyframe/confirm")
    assert r.status_code in (404, 405), r.text
    r2 = client.post(f"/api/shots/{ctx['shot']['id']}/keyframe/unconfirm")
    assert r2.status_code in (404, 405), r2.text
    r3 = client.post(f"/api/projects/{ctx['shot']['id']}/keyframe/confirm", json={})
    assert r3.status_code in (404, 405), r3.text


def test_clip_passes_without_confirmation(client: TestClient, mock_render):
    """生成 mock 分镜图后即可生成 clip——不再需要逐镜人工确认。"""
    ctx = _make_shot(client)
    sid = ctx["shot"]["id"]
    wf = _workflow(client, "gate-image", "image", _IMAGE_WF)
    j = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True

    # 未调用 /keyframe/confirm，直接生成视频
    detail = client.get(f"/api/shots/{sid}").json()
    assert detail["keyframe_confirmed"] is False
    vwf = _workflow(client, "gate-video", "video", _VIDEO_WF)
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "clip", "workflow_id": vwf})
    assert r.status_code == 202, r.text


def test_clip_blocked_when_keyframe_file_missing(client: TestClient, mock_render):
    """分镜图记录存在但文件被删除 → 生成 clip 被拦截（不能只看数据库字段）。"""
    ctx = _make_shot(client)
    sid = ctx["shot"]["id"]
    wf = _workflow(client, "gate-image-mf", "image", _IMAGE_WF)
    j = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True

    from app.core.database import SessionLocal
    from app.models import Asset
    _db = SessionLocal()
    try:
        a = _db.query(Asset).filter(Asset.asset_type == "keyframe").first()
        # 直接改成一个绝对不存在的路径：等价于文件被清理/迁移丢失，
        # 避免依赖 stored_path 的相对/绝对口径差异
        a.file_path = "/nonexistent/dlab_test/gone_keyframe.png"
        _db.commit()
    finally:
        _db.close()

    vwf = _workflow(client, "gate-video-mf", "video", _VIDEO_WF)
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "clip", "workflow_id": vwf})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "keyframe_not_ready"


def test_clip_blocked_when_keyframe_superseded(client: TestClient, mock_render):
    """分镜图版本已失效（重绘后旧版）→ 生成 clip 被拦截。"""
    ctx = _make_shot(client)
    sid = ctx["shot"]["id"]
    wf = _workflow(client, "gate-image-sp", "image", _IMAGE_WF)
    j = client.post(f"/api/shots/{sid}/generate", json={"target": "keyframe", "workflow_id": wf}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True

    from app.core.database import SessionLocal
    from app.models import Asset
    _db = SessionLocal()
    try:
        a = _db.query(Asset).filter(Asset.asset_type == "keyframe").first()
        a.validity = "superseded"
        _db.commit()
    finally:
        _db.close()

    vwf = _workflow(client, "gate-video-sp", "video", _VIDEO_WF)
    r = client.post(f"/api/shots/{sid}/generate", json={"target": "clip", "workflow_id": vwf})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "keyframe_not_ready"


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
    new_id = body["retried"][0]["new_job_id"]
    lst = client.get(f"/api/jobs?project_id={pid}&status=queued").json()
    assert new_id in [i["id"] for i in lst["items"]]
