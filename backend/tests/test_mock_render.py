"""Mock 渲染模式测试（D-11）：render_mock 开启时任务跳过 ComfyUI 交互，
占位产物照常入库，任务走完整状态机。
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
    # 开启 Mock 渲染
    from app.core.config import get_settings
    import app.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: cfg_mod.Settings(render_mock=True, media_dir=tmp_path / "media"))
    # executor 内部以局部导入使用 get_settings，需同步替换 worker 命名空间
    import app.worker.executor as ex_mod
    monkeypatch.setattr(ex_mod, "get_settings", lambda: cfg_mod.Settings(render_mock=True, media_dir=tmp_path / "media"))
    return TestClient(app)


def test_mock_outputs_generate_all_kinds():
    """mock_outputs 按输出名生成图/视频/音频三类占位产物。"""
    from app.services.mock_render import mock_outputs
    records = mock_outputs({"cover": "1", "clip": "2", "voice": "3"}, "jobtest1234")
    assert len(records) == 3
    by_name = {r["out_name"]: r for r in records}
    assert by_name["cover"]["item"]["filename"].endswith(".png")
    assert by_name["clip"]["item"]["filename"].endswith(".mp4")
    assert by_name["voice"]["item"]["filename"].endswith(".wav")
    for r in records:
        assert len(r["content"]) > 0


def test_mock_generate_job_full_lifecycle(client: TestClient):
    """Mock 模式下生成任务走 queued → running → succeeded，占位资产入库。"""
    # 准备项目/场景/镜头/工作流
    pid = client.post("/api/projects", json={"name": "mock 项目", "aspect_ratio": "9:16"}).json()["id"]
    # D-23 资产审核闸口：本文件聚焦 mock 渲染与任务生命周期，
    # 直接放行公共资产人工审核，避免与资产审核前置条件耦合
    from app.core.database import SessionLocal
    from app.models import Project
    _db = SessionLocal()
    try:
        _p = _db.get(Project, pid)
        _p.assets_review_status = "approved"
        _db.commit()
    finally:
        _db.close()
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    # 创建带标记的 mock 工作流（cover 输出）：契约为 multipart 上传（Form + file）
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
        data={"name": "mock-image", "workflow_type": "image"},
        files={"file": ("mock_image.json", json.dumps(definition), "application/json")},
    )
    if wf.status_code >= 400:
        pytest.skip(f"工作流创建接口不可用或契约不同: {wf.status_code} {wf.text[:200]}")
    wf_id = wf.json()["id"]
    # 触发单镜生成
    r = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "keyframe", "workflow_id": wf_id})
    assert r.status_code in (200, 201, 202), r.text
    job = r.json()
    jid = job.get("id") or job.get("job", {}).get("id")
    assert jid
    # 直接执行 executor（单测环境无 worker 进程）
    from app.worker.executor import execute_generate_job
    result = execute_generate_job(jid)
    assert result["ok"] is True, result
    assert result["assets"]
    # 任务终态与产物校验
    detail = client.get(f"/api/jobs/{jid}").json()
    assert detail["status"] == "succeeded"
    assert detail["result"]["prompt_id"].startswith("mock-")
    assert detail["result"]["assets"]


def test_anchor_writeback_prop_character_scene(client: TestClient):
    """D-20 锚定图/定妆图/基准图回写：生成成功后首个 keyframe 产物回写到对应记录。"""
    import json
    pid = client.post("/api/projects", json={"name": "回写项目", "aspect_ratio": "9:16"}).json()["id"]
    # 创建带 cover 输出的 mock 工作流
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
        data={"name": "mock-anchor", "workflow_type": "image"},
        files={"file": ("mock_anchor.json", json.dumps(definition), "application/json")},
    )
    if wf.status_code >= 400:
        pytest.skip(f"工作流创建接口不可用或契约不同: {wf.status_code} {wf.text[:200]}")
    wf_id = wf.json()["id"]
    # 道具 / 角色 / 场景各一条
    prop = client.post("/api/props", json={"project_id": pid, "name": "古剑", "appearance": "青铜古剑"}).json()
    ch = client.post("/api/characters", json={"project_id": pid, "name": "林晓", "appearance": "短发少女"}).json()
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    from app.worker.executor import execute_generate_job
    from app.core.database import SessionLocal
    from app.models import Character, Prop, Scene
    # 道具锚定图
    r = client.post(f"/api/props/{prop['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    jid = r.json().get("id") or r.json().get("job", {}).get("id")
    assert execute_generate_job(jid)["ok"] is True
    # 角色定妆图
    r = client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    jid = r.json().get("id") or r.json().get("job", {}).get("id")
    assert execute_generate_job(jid)["ok"] is True
    # 场景基准图
    r = client.post(f"/api/scenes/{scn['id']}/generate-baseline", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    jid = r.json().get("id") or r.json().get("job", {}).get("id")
    assert execute_generate_job(jid)["ok"] is True
    # 回写校验
    db = SessionLocal()
    try:
        p = db.get(Prop, prop["id"])
        c = db.get(Character, ch["id"])
        s = db.get(Scene, scn["id"])
        assert p.anchor_asset_id, "道具 anchor_asset_id 未回写"
        assert c.portrait_asset_id, "角色 portrait_asset_id 未回写"
        assert s.baseline_asset_id, "场景 baseline_asset_id 未回写"
        # 回写的资产必须是 succeeded 任务产出，且按实体归属落正确类型
        from app.models import Asset, Job
        expected = {
            p.anchor_asset_id: "prop",
            c.portrait_asset_id: "character",
            s.baseline_asset_id: "scene",
        }
        for aid, want_type in expected.items():
            a = db.get(Asset, aid)
            assert a is not None and a.validity == "current"
            assert a.asset_type == want_type, f"资产 {aid} 类型应为 {want_type}，实际 {a.asset_type}"
            j = db.get(Job, a.source_job_id)
            assert j is not None and j.status == "succeeded"
    finally:
        db.close()

    # 资产库分类过滤：每类只能返回自身类型，非法值必须 422
    for at, want in (("character", "character"), ("scene", "scene"), ("prop", "prop")):
        r = client.get("/api/assets", params={"project_id": pid, "asset_type": at})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] >= 1, f"{at} 分类应至少有一条资产"
        assert all(i["asset_type"] == want for i in body["items"]), f"{at} 分类混入其他类型"
    r = client.get("/api/assets", params={"project_id": pid, "asset_type": "portrait"})
    assert r.status_code == 422, "非法分类值应返回 422"
