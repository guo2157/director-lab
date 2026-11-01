"""clip 自动参数注入与在途任务去重测试。

覆盖（WORKFLOW.md §5.2/§8/§13.1）：
- clip 任务自动注入 ref_image（当前镜头分镜图）/ ref_prev_frame（上一镜头末帧）
- 按 project.aspect_ratio 注入 H3 width/height（9:16 → 736x1312 等）
- 按 shot.duration 对齐帧数 length（fps=24，步长 17，offset 5，范围 124-362）
- 在途任务去重：同镜头同目标 queued/running 任务存在时，单镜生成/重试返回 409 job_in_flight
- 批量生成跳过在途镜头并在响应 skipped 中报告
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


_IMAGE_DEF = {
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


def _make_project(client, aspect_ratio="9:16"):
    pid = client.post("/api/projects", json={"name": "注入测试", "aspect_ratio": aspect_ratio}).json()["id"]
    # D-23 资产审核闸口：本文件聚焦参数注入与在途去重，
    # 直接放行公共资产人工审核，避免与资产审核前置条件耦合
    from app.core.database import SessionLocal
    from tests.conftest import approve_project_assets
    _db = SessionLocal()
    try:
        approve_project_assets(_db, pid)
    finally:
        _db.close()
    return pid


def _make_shot(client, pid, duration=5, episode=1):
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "草地", "episode": episode}).json()
    # D-23 资产门禁：为场景构造真实基准图资产（指针 → current → 文件存在）
    from app.core.database import SessionLocal
    from app.models import Scene
    from tests.conftest import seed_scene_baseline_asset
    _db = SessionLocal()
    try:
        _scene = _db.get(Scene, scn["id"])
        seed_scene_baseline_asset(_db, _scene, "/tmp/dlab_test_media")
    finally:
        _db.close()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": episode, "shot_kind": "visual", "duration": duration,
    }).json()
    return scn, shot


def _make_workflow(client, wf_type, name):
    return client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": wf_type},
        files={"file": (f"{name}.json", json.dumps(_IMAGE_DEF), "application/json")},
    ).json()


def _gen_keyframe_and_confirm(client, shot_id, wf_id, monkeypatch):
    import app.worker.executor as ex_mod
    from app.core.config import Settings
    monkeypatch.setattr(ex_mod, "get_settings", lambda: Settings(render_mock=True))
    j = client.post(f"/api/shots/{shot_id}/generate", json={"target": "keyframe", "workflow_id": wf_id}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    assert execute_generate_job(jid)["ok"] is True
    r = client.post(f"/api/shots/{shot_id}/keyframe/confirm")
    assert r.status_code == 200, r.text


def _run_clip_job(client, shot_id, wf_id, monkeypatch):
    """创建并执行 clip 任务，返回 (job_id, 执行结果)。"""
    import app.worker.executor as ex_mod
    from app.core.config import Settings
    monkeypatch.setattr(ex_mod, "get_settings", lambda: Settings(render_mock=True))
    j = client.post(f"/api/shots/{shot_id}/generate", json={"target": "clip", "workflow_id": wf_id}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    result = execute_generate_job(jid)
    return jid, result


# ---------- clip 自动参数注入 ----------

def test_clip_injects_ref_image_and_wh_length(client, monkeypatch):
    """clip 任务自动注入 ref_image（分镜图）、width/height（9:16）与 length（时长换算）。"""
    pid = _make_project(client, "9:16")
    _, shot = _make_shot(client, pid, duration=5)
    wf = _make_workflow(client, "image", "inj-image")
    _gen_keyframe_and_confirm(client, shot["id"], wf["id"], monkeypatch)
    jid, result = _run_clip_job(client, shot["id"], wf["id"], monkeypatch)
    assert result["ok"] is True, result
    # 校验注入结果：从数据库读取任务 input_snapshot
    import app.core.database as db_mod
    from app.models import Job
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=db_mod.engine)()
    job = s.get(Job, jid)
    snap = job.input_snapshot or {}
    injected = snap.get("injected_params", {})
    # width/height 按 9:16 注入
    assert injected.get("width") == 736 and injected.get("height") == 1312
    # length：duration=5s → 5*24=120 → n=round((120-5)/17)=7 → 17*7+5=124
    assert injected.get("length") == 124
    # ref_image 注入了分镜图路径
    assert snap.get("injected_media", {}).get("ref_image")
    s.close()


def test_clip_length_clamped_to_range(client, monkeypatch):
    """duration 超范围时 length 收敛到 [124, 362]。"""
    pid = _make_project(client, "16:9")
    _, shot = _make_shot(client, pid, duration=30)
    wf = _make_workflow(client, "image", "clamp-image")
    _gen_keyframe_and_confirm(client, shot["id"], wf["id"], monkeypatch)
    jid, result = _run_clip_job(client, shot["id"], wf["id"], monkeypatch)
    assert result["ok"] is True, result
    import app.core.database as db_mod
    from app.models import Job
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=db_mod.engine)()
    job = s.get(Job, jid)
    injected = (job.input_snapshot or {}).get("injected_params", {})
    # 16:9 → 1312x736；30s → 720 → n=min(21, round(715/17))=21 → 362
    assert injected.get("width") == 1312 and injected.get("height") == 736
    assert injected.get("length") == 362
    s.close()


# ---------- 在途任务去重 ----------

def test_generate_rejected_while_inflight(client, monkeypatch):
    """同镜头同目标已有 queued 任务时，再次生成返回 409 job_in_flight。"""
    pid = _make_project(client)
    _, shot = _make_shot(client, pid)
    wf = _make_workflow(client, "image", "dup-image")
    _gen_keyframe_and_confirm(client, shot["id"], wf["id"], monkeypatch)
    # 第一次提交 clip（queued，不执行）
    r1 = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "clip", "workflow_id": wf["id"]})
    assert r1.status_code == 202, r1.text
    # 第二次提交同目标 → 409
    r2 = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "clip", "workflow_id": wf["id"]})
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "job_in_flight"
    # 不同目标（keyframe）不受影响
    r3 = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "keyframe", "workflow_id": wf["id"]})
    assert r3.status_code == 202


def test_retry_rejected_while_inflight(client, monkeypatch):
    """同镜头同目标在途时，重试失败任务返回 409 job_in_flight。"""
    pid = _make_project(client)
    _, shot = _make_shot(client, pid)
    wf = _make_workflow(client, "image", "retry-image")
    _gen_keyframe_and_confirm(client, shot["id"], wf["id"], monkeypatch)
    # 创建一个 clip 任务并直接置为 failed（不执行）
    r1 = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "clip", "workflow_id": wf["id"]})
    jid = r1.json()["id"]
    import app.core.database as db_mod
    from app.models import Job
    from sqlalchemy.orm import sessionmaker
    s = sessionmaker(bind=db_mod.engine)()
    s.get(Job, jid).status = "failed"
    s.commit()
    s.close()
    # 再创建一个在途 clip 任务
    r2 = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "clip", "workflow_id": wf["id"]})
    assert r2.status_code == 202
    # 重试失败任务 → 409（在途冲突）
    r3 = client.post(f"/api/jobs/{jid}/retry")
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "job_in_flight"


def test_batch_generate_skips_inflight(client, monkeypatch):
    """批量生成跳过在途镜头，响应 skipped 报告原因。"""
    pid = _make_project(client)
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "多镜场景", "episode": 1}).json()
    # D-23 资产门禁：为场景构造真实基准图资产
    from app.core.database import SessionLocal
    from app.models import Scene
    from tests.conftest import seed_scene_baseline_asset
    _db = SessionLocal()
    try:
        _scene = _db.get(Scene, scn["id"])
        seed_scene_baseline_asset(_db, _scene, "/tmp/dlab_test_media")
    finally:
        _db.close()
    shot_ids = []
    for i in range(3):
        sh = client.post("/api/shots", json={
            "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 4,
        }).json()
        shot_ids.append(sh["id"])
    wf = _make_workflow(client, "image", "batch-image")
    # 第一个镜头已有在途 keyframe 任务
    r0 = client.post(f"/api/shots/{shot_ids[0]}/generate", json={"target": "keyframe", "workflow_id": wf["id"]})
    assert r0.status_code == 202
    # 场景批量生成：跳过镜头 1，生成镜头 2/3
    r = client.post(f"/api/scenes/{scn['id']}/generate", json={"target": "keyframe", "workflow_id": wf["id"]})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["count"] == 2
    assert len(body.get("skipped", [])) == 1
    assert body["skipped"][0]["shot_id"] == shot_ids[0]
    assert body["skipped"][0]["reason"] == "job_in_flight"
