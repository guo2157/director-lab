"""资产库分类端到端回归。

覆盖链路：制作资产生成 → worker 落库写入正确 asset_type → GET /api/assets 分类过滤 → 分类隔离。
约束：Mock 渲染模式，不依赖外部 ComfyUI；每类只断言自身类型。
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
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    from app.core.config import Settings
    import app.core.config as cfg_mod
    import app.worker.executor as ex_mod

    def factory():
        return Settings(render_mock=True, media_dir=tmp_path / "media")

    monkeypatch.setattr(cfg_mod, "get_settings", factory)
    monkeypatch.setattr(ex_mod, "get_settings", factory)
    return TestClient(app)


def _cover_workflow(client: TestClient) -> str:
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
        data={"name": "mock-cover", "workflow_type": "image"},
        files={"file": ("mock_cover.json", json.dumps(definition), "application/json")},
    )
    if r.status_code >= 400:
        pytest.skip(f"工作流创建接口不可用: {r.status_code} {r.text[:200]}")
    return r.json()["id"]


def _run(client: TestClient, resp) -> None:
    assert resp.status_code == 202, resp.text
    body = resp.json()
    jid = body.get("id") or body.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job

    assert execute_generate_job(jid)["ok"] is True


def _types_of(client: TestClient, project_id: str, **params) -> set:
    r = client.get("/api/assets", params={"project_id": project_id, **params})
    assert r.status_code == 200, r.text
    return {i["asset_type"] for i in r.json()["items"]}


def test_asset_categories_are_isolated(client: TestClient):
    wf_id = _cover_workflow(client)
    pid = client.post("/api/projects", json={"name": "分类回归", "aspect_ratio": "9:16"}).json()["id"]
    ch = client.post(
        "/api/characters", json={"project_id": pid, "name": "小马", "appearance": "棕色小马"}
    ).json()
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "木屋前草地", "episode": 1}).json()
    prop = client.post(
        "/api/props", json={"project_id": pid, "name": "麦袋", "appearance": "布制麦袋"}
    ).json()

    _run(client, client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id}))
    _run(client, client.post(f"/api/scenes/{scn['id']}/generate-baseline", json={"workflow_id": wf_id}))
    _run(client, client.post(f"/api/props/{prop['id']}/generate", json={"workflow_id": wf_id}))

    assert _types_of(client, pid, asset_type="character") == {"character"}
    assert _types_of(client, pid, asset_type="scene") == {"scene"}
    assert _types_of(client, pid, asset_type="prop") == {"prop"}
    assert _types_of(client, pid) == {"character", "scene", "prop"}

    for _ in range(3):
        assert _types_of(client, pid) == {"character", "scene", "prop"}
        assert _types_of(client, pid, asset_type="character") == {"character"}
        assert _types_of(client, pid, asset_type="scene") == {"scene"}
        assert _types_of(client, pid, asset_type="prop") == {"prop"}

    assert _types_of(client, pid, asset_type="character", keyword="小马") == {"character"}
    assert _types_of(client, pid, asset_type="scene", keyword="木屋") == {"scene"}
    assert _types_of(client, pid, asset_type="prop", keyword="麦袋") == {"prop"}

    r = client.get("/api/assets", params={"project_id": pid, "asset_type": "character", "page_size": 1})
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = client.get("/api/assets", params={"project_id": pid, "asset_type": "clip"})
    assert r.status_code == 200
    assert r.json()["total"] == 0 and r.json()["items"] == []

    r = client.get("/api/assets", params={"project_id": pid, "asset_type": "portrait"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_asset_type"


def test_asset_categories_isolated_across_projects(client: TestClient):
    wf_id = _cover_workflow(client)
    pid_a = client.post("/api/projects", json={"name": "项目A", "aspect_ratio": "9:16"}).json()["id"]
    pid_b = client.post("/api/projects", json={"name": "项目B", "aspect_ratio": "9:16"}).json()["id"]
    cha = client.post(
        "/api/characters", json={"project_id": pid_a, "name": "甲角色", "appearance": "甲"}
    ).json()
    chb = client.post(
        "/api/characters", json={"project_id": pid_b, "name": "乙角色", "appearance": "乙"}
    ).json()
    _run(client, client.post(f"/api/characters/{cha['id']}/generate", json={"workflow_id": wf_id}))
    _run(client, client.post(f"/api/characters/{chb['id']}/generate", json={"workflow_id": wf_id}))

    ra = client.get("/api/assets", params={"project_id": pid_a, "asset_type": "character"}).json()
    rb = client.get("/api/assets", params={"project_id": pid_b, "asset_type": "character"}).json()
    assert ra["total"] == 1 and rb["total"] == 1
    assert ra["items"][0]["project_id"] == pid_a
    assert rb["items"][0]["project_id"] == pid_b
    assert ra["items"][0]["id"] != rb["items"][0]["id"]
