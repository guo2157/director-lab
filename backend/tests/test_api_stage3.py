"""阶段 3 接口集成测试：项目/角色/道具/场景/镜头核心契约。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine, SessionLocal
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


def _make_project(client: TestClient) -> str:
    r = client.post("/api/projects", json={"name": "测试项目", "aspect_ratio": "9:16"})
    assert r.status_code == 201
    return r.json()["id"]


def test_project_lifecycle(client):
    pid = _make_project(client)
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["status"] == "draft"
    assert detail["current_stage"] == "estimate"
    assert detail["shot_total"] == 0
    # draft 不可归档
    assert client.post(f"/api/projects/{pid}/archive").status_code == 409
    # 项目列表
    lst = client.get("/api/projects").json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == pid


def test_character_uniqueness_and_rename(client):
    pid = _make_project(client)
    r = client.post("/api/characters", json={"project_id": pid, "name": "林晓", "appearance": "短发"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert client.post("/api/characters", json={"project_id": pid, "name": "林晓"}).status_code == 409
    # 建场景与镜头，镜头提示词含 @林晓
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "dialogue",
        "dialogue": "你好", "duration": 5, "image_prompt": "教室里的 @林晓",
    }).json()
    # 改名后 @引用 同步改写
    client.patch(f"/api/characters/{cid}", json={"name": "林小晓"})
    detail = client.get(f"/api/shots/{shot['id']}").json()
    assert detail["image_prompt"] == "教室里的 @林小晓"


def test_shot_creation_rules(client):
    pid = _make_project(client)
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "操场", "episode": 1}).json()
    # dialogue 无对白 → 400
    assert client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "dialogue", "duration": 5,
    }).status_code == 400
    # 场景未出现的集 → 422
    assert client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 9, "shot_kind": "visual", "duration": 5,
    }).status_code == 422
    # 正常创建
    r = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    })
    assert r.status_code == 201 and r.json()["seq"] == 1


def test_shot_duration_warning_and_aggregate(client):
    pid = _make_project(client)
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "天台", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "narration",
        "dialogue": "旁白", "duration": 5,
    }).json()
    # 时长超出 H3 范围 → warning 但仍接受
    r = client.patch(f"/api/shots/{shot['id']}", json={"duration": 30})
    assert r.status_code == 200
    assert r.json()["duration"] == 30
    assert r.json()["warning"] == "duration_out_of_range"
    # 详情聚合结构
    detail = client.get(f"/api/shots/{shot['id']}").json()
    assert detail["scene"]["title"] == "天台"
    assert set(detail["current_assets"].keys()) == {"keyframe", "clip", "audio"}
    # 项目按集拉取
    lst = client.get(f"/api/projects/{pid}/shots", params={"episode": 1}).json()
    assert lst["total"] == 1 and lst["items"][0]["scene_title"] == "天台"


def test_scene_episode_management(client):
    pid = _make_project(client)
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "图书馆", "episode": 1}).json()
    sid = scn["id"]
    assert scn["episodes"] == [1]
    # 追加集数
    r = client.post(f"/api/scenes/{sid}/episodes", json={"episode": 3, "seq_in_episode": 1})
    assert r.status_code == 201 and r.json()["requires_parse"] is True
    # 重复追加 → 409
    assert client.post(f"/api/scenes/{sid}/episodes", json={"episode": 3, "seq_in_episode": 1}).status_code == 409
    # 按集筛选场景
    eps3 = client.get(f"/api/projects/{pid}/scenes", params={"episode": 3}).json()
    assert len(eps3) == 1
    eps2 = client.get(f"/api/projects/{pid}/scenes", params={"episode": 2}).json()
    assert len(eps2) == 0


def test_prop_crud(client):
    pid = _make_project(client)
    r = client.post("/api/props", json={"project_id": pid, "name": "旧怀表", "appearance": "黄铜外壳"})
    assert r.status_code == 201
    assert client.post("/api/props", json={"project_id": pid, "name": "旧怀表"}).status_code == 409
    lst = client.get(f"/api/projects/{pid}/props").json()
    assert len(lst) == 1 and lst[0]["is_key_prop"] is False
