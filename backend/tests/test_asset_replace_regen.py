"""资产上传 / 替换 / 重新生成接口回归测试（Mock 渲染，不依赖外部 ComfyUI）。"""
import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import make_engine
from app.models import Base

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    from app.core.config import Settings
    import app.core.config as cfg_mod
    import app.worker.executor as ex_mod

    def factory():
        return Settings(render_mock=True, media_dir=tmp_path / "media")

    monkeypatch.setattr(cfg_mod, "get_settings", factory)
    monkeypatch.setattr(ex_mod, "get_settings", factory)
    import app.api.v1.projects as pj
    monkeypatch.setattr(pj, "_check_runtime_config", lambda: None)
    return TestClient(app)


def _project(client) -> str:
    return client.post("/api/projects", json={"name": "资产替换项目", "aspect_ratio": "9:16"}).json()["id"]


def _image_workflow(client) -> str:
    """创建最小可用图片工作流（image 类型），供重新生成匹配。"""
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
    r = client.post(
        "/api/workflows",
        data={"name": "regen-cover", "workflow_type": "image"},
        files={"file": ("regen_cover.json", json.dumps(definition), "application/json")},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _upload(client, pid, asset_type, name="封面"):
    return client.post(
        "/api/assets/upload",
        data={"project_id": pid, "asset_type": asset_type, "name": name},
        files={"file": ("a.png", PNG, "image/png")},
    )


def test_upload_requires_asset_type(client):
    pid = _project(client)
    r = client.post(
        "/api/assets/upload",
        data={"project_id": pid},
        files={"file": ("a.png", PNG, "image/png")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_asset_type"


def test_upload_lands_in_category(client):
    pid = _project(client)
    r = _upload(client, pid, "character")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["asset_type"] == "character"
    assert body["version"] == 1
    listed = client.get("/api/assets", params={"project_id": pid, "asset_type": "character"}).json()
    assert any(i["id"] == body["id"] for i in listed["items"])


def test_upload_rejects_wrong_file_kind(client):
    pid = _project(client)
    r = client.post(
        "/api/assets/upload",
        data={"project_id": pid, "asset_type": "clip"},
        files={"file": ("a.png", PNG, "image/png")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_file_type"


def test_replace_overwrites_in_place(client):
    """replace 为原地覆盖语义（2026-10-28 改版）：不产生新版本，
    资产 id/name/file_path/version 不变，文件内容与 updated_at 更新。"""
    pid = _project(client)
    first = _upload(client, pid, "character").json()
    r = client.post(
        f"/api/assets/{first['id']}/replace",
        files={"file": ("b.png", PNG, "image/png")},
    )
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] == first["id"]
    assert new["version"] == first["version"]
    assert new["validity"] == "current"
    assert new["file_path"] == first["file_path"]
    assert new["name"] == first["name"]
    # 文件内容已被覆盖更新
    from pathlib import Path
    assert Path(new["file_path"]).read_bytes() == PNG


def test_replace_locked_asset_conflicts(client):
    pid = _project(client)
    first = _upload(client, pid, "prop").json()
    client.post(f"/api/assets/{first['id']}/lock")
    r = client.post(
        f"/api/assets/{first['id']}/replace",
        files={"file": ("b.png", PNG, "image/png")},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "asset_locked"


def test_replace_rejects_type_mismatch(client):
    pid = _project(client)
    first = _upload(client, pid, "scene", name="操场").json()
    r = client.post(
        f"/api/assets/{first['id']}/replace",
        files={"file": ("b.mp4", b"0000", "video/mp4")},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "invalid_file_type"


def test_regenerate_config_only_for_entity_assets(client):
    pid = _project(client)
    kf = _upload(client, pid, "keyframe", name="分镜图").json()
    r = client.get(f"/api/assets/{kf['id']}/regenerate/config")
    assert r.status_code == 422, r.text


def test_regenerate_config_returns_prompt(client):
    pid = _project(client)
    ch = client.post("/api/characters", json={"project_id": pid, "name": "小明", "appearance": "短发男孩"}).json()
    asset = _upload(client, pid, "character", name="小明").json()
    from app.core.database import SessionLocal
    from app.models import Character
    db = SessionLocal()
    obj = db.get(Character, ch["id"])
    obj.portrait_asset_id = asset["id"]
    db.commit()
    db.close()
    r = client.get(f"/api/assets/{asset['id']}/regenerate/config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asset_type"] == "character"
    assert "小明" in body["prompt"]
    assert body["editable"]["refs"] is False
    assert body["editable"]["workflow"] is False


def test_regenerate_submits_job_with_prompt(client):
    _image_workflow(client)
    pid = _project(client)
    ch = client.post("/api/characters", json={"project_id": pid, "name": "小红", "appearance": "长发女孩"}).json()
    asset = _upload(client, pid, "character", name="小红").json()
    from app.core.database import SessionLocal
    from app.models import Character, Job
    db = SessionLocal()
    obj = db.get(Character, ch["id"])
    obj.portrait_asset_id = asset["id"]
    db.commit()
    db.close()
    r = client.post(
        f"/api/assets/{asset['id']}/regenerate",
        json={"prompt": "自定义提示词", "idempotency_key": "k1"},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["asset_id"] == asset["id"]
    assert body["prompt_edited"] is True
    dup = client.post(
        f"/api/assets/{asset['id']}/regenerate",
        json={"prompt": "自定义提示词", "idempotency_key": "k1"},
    )
    assert dup.status_code == 202, dup.text
    assert dup.json().get("idempotent") is True
    db = SessionLocal()
    job = db.get(Job, body["job_id"])
    assert job.params["prompt"] == "自定义提示词"
    assert (job.input_snapshot or {}).get("regeneration", {}).get("asset_id") == asset["id"]
    db.close()


def test_regenerate_locked_conflicts(client):
    _image_workflow(client)
    pid = _project(client)
    ch = client.post("/api/characters", json={"project_id": pid, "name": "小刚", "appearance": "寸头"}).json()
    asset = _upload(client, pid, "character", name="小刚").json()
    from app.core.database import SessionLocal
    from app.models import Character
    db = SessionLocal()
    obj = db.get(Character, ch["id"])
    obj.portrait_asset_id = asset["id"]
    db.commit()
    db.close()
    client.post(f"/api/assets/{asset['id']}/lock")
    r = client.post(f"/api/assets/{asset['id']}/regenerate", json={})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "asset_locked"
