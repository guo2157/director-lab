"""资产定义接口实测：查询、批量生成与各环节闸口。

验证点：
- 解析确认后可查询到五类定义，计数准确
- 公共资产未审核通过时，分镜图按定义生成返回 409 assets_review_pending
- 公共资产（角色）按定义生成放行 202
- 分镜图审核通过后可批量生成，已锁定分镜图被跳过
- 分镜视频闸口：分镜图未确认时返回 409 keyframe_not_confirmed
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
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    import app.worker.parse_executor as pe_mod
    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    return TestClient(app)


SCRIPT_TEXT = "第一集。林晓走进教室，对同桌说：你看见我的笔记本了吗？ 同桌摇头。窗外下起了雨。" * 30

FAKE_PARSE_OUTPUT = {
    "project_analysis": {"summary": "教室悬疑", "theme": "信任", "highlights": ["反转"], "foreshadowing": []},
    "characters": [{"name": "林晓", "description": "转学生", "traits": "细心", "appearance": "短发"}],
    "props": [{"name": "笔记本", "description": "蓝色封面", "appearance": "磨损"}],
    "scenes": [{
        "episode": 1, "title": "教室", "description": "午后教室", "time_note": "白天",
        "shots": [
            {"episode": 1, "seq": 1, "scene_id": "教室", "character_id": "林晓",
             "dialogue": "你看见我的笔记本了吗？", "duration": 5,
             "image_prompt": "教室里的林晓", "shot_kind": "dialogue"},
            {"episode": 1, "seq": 2, "scene_id": "教室", "duration": 4,
             "image_prompt": "窗外的雨", "shot_kind": "visual"},
        ],
    }],
    "episodes": [{"episode": 1, "goal": "建立悬念", "rhythm_curve": "平稳",
                  "cliffhanger_type": "信息缺失", "hook_to_next": "笔记本下落", "locked": False}],
}

IMAGE_WF = {
    "1": {"class_type": "EmptyLatentImage", "_meta": {"title": "$~width, $~height"},
          "inputs": {"width": 512, "height": 288, "batch_size": 1}},
    "2": {"class_type": "SaveImage", "_meta": {"title": "$output.cover"},
          "inputs": {"images": ["1", 0]}},
}


def _prepare(client, monkeypatch):
    pid = client.post("/api/projects", json={"name": "定义接口", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": SCRIPT_TEXT}).json()
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    job_id = r.json()["job_id"]
    import app.worker.parse_executor as pe_mod

    class _FakeLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, s, u):
            return FAKE_PARSE_OUTPUT

    monkeypatch.setattr(pe_mod, "LLMClient", _FakeLLM)
    assert pe_mod.execute_parse_job(job_id)["ok"] is True
    pv_id = client.get(f"/api/jobs/{job_id}").json()["result"]["parse_version_id"]
    client.post(f"/api/projects/{pid}/parse-versions/{pv_id}/confirm",
                json={"current_parse_version_id": None})
    wf = client.post("/api/workflows", data={"name": "plan-image", "workflow_type": "image"},
                     files={"file": ("p.json", json.dumps(IMAGE_WF), "application/json")})
    assert wf.status_code == 201, wf.text
    return pid, wf.json()["id"]


def _approve(pid):
    from app.core.database import SessionLocal
    from app.models import Project
    db = SessionLocal()
    try:
        p = db.get(Project, pid)
        p.assets_review_status = "approved"
        db.commit()
    finally:
        db.close()


def _seed_assets(pid):
    """D-23 资产门禁：为项目下所有角色构造真实定妆图、所有场景构造真实基准图。

    门禁校验资产真实状态（指针 → current → 文件存在），仅设审核标志不再足够。
    """
    from pathlib import Path
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, ids
    from tests.conftest import seed_scene_baseline_asset
    db = SessionLocal()
    try:
        for ch in db.query(Character).filter(Character.project_id == pid).all():
            fpath = Path(f"/tmp/dlab_test_media/portrait_{ch.id}.png")
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            a = Asset(id=ids.asset_id(), project_id=pid, asset_type="character",
                      name=f"{ch.name}-定妆图", version=1, file_path=str(fpath), validity="current")
            db.add(a)
            db.flush()
            ch.portrait_asset_id = a.id
        for sc in db.query(Scene).filter(Scene.project_id == pid).all():
            seed_scene_baseline_asset(db, sc, "/tmp/dlab_test_media")
        db.commit()
    finally:
        db.close()


def test_list_plans_after_parse(client, monkeypatch):
    """解析确认后可查询到五类定义，计数与内容正确。"""
    pid, _ = _prepare(client, monkeypatch)
    r = client.get(f"/api/projects/{pid}/asset-plans", params={"with_dependencies": True})
    assert r.status_code == 200, r.text
    body = r.json()
    counts = body["counts"]
    assert counts["character"]["total"] == 1
    assert counts["scene"]["total"] == 1
    assert counts["prop"]["total"] == 1
    assert counts["keyframe"]["total"] == 2
    assert counts["clip"]["total"] == 2
    assert all(v["pending"] == v["total"] for v in counts.values())
    # 分镜图定义带依赖，且提示词引用参考名
    kf = [x for x in body["items"] if x["plan_type"] == "keyframe"][0]
    assert kf["dependencies"], "分镜图定义缺少依赖"
    assert "ref_scene" in {d["slot"] for d in kf["dependencies"]}


def test_keyframe_blocked_without_review(client, monkeypatch):
    """公共资产未审核通过时，分镜图按定义生成被拦截。"""
    pid, wf = _prepare(client, monkeypatch)
    # D-23：先补齐真实资产（资产完整但未审核 → assets_review_pending）
    _seed_assets(pid)
    r = client.post(f"/api/projects/{pid}/asset-plans/generate",
                    json={"plan_type": "keyframe", "workflow_id": wf})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "assets_review_pending"


def test_character_generate_allowed(client, monkeypatch):
    """公共资产（角色）按定义生成放行，返回 202 与任务。"""
    pid, wf = _prepare(client, monkeypatch)
    r = client.post(f"/api/projects/{pid}/asset-plans/generate",
                    json={"plan_type": "character", "workflow_id": wf})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


def test_keyframe_generate_after_review(client, monkeypatch):
    """审核通过后分镜图可批量生成。"""
    pid, wf = _prepare(client, monkeypatch)
    _seed_assets(pid)
    _approve(pid)
    r = client.post(f"/api/projects/{pid}/asset-plans/generate",
                    json={"plan_type": "keyframe", "workflow_id": wf})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 2


def test_clip_blocked_without_keyframe_confirm(client, monkeypatch):
    """分镜图未确认时，分镜视频按定义生成被闸口拦截。"""
    pid, wf = _prepare(client, monkeypatch)
    _approve(pid)
    r = client.post(f"/api/projects/{pid}/asset-plans/generate",
                    json={"plan_type": "clip", "workflow_id": wf})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "keyframe_not_confirmed"
