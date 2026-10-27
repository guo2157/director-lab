"""第四期修订测试（D-16 补充 + D-18 并行调度）。
多角色注入：
- 多个 @角色 按出现顺序填入 ref_identity → ref_image → ref_costume
- 槽位不足返回 422，不静默丢参考
- 角色与场景同名（历史数据兜底）返回 422
同名校验：
- 创建/改名角色时与场景同名返回 409
- 创建/改名场景时与角色同名返回 409
端点补齐：
- 批量生成与 regenerate 端点携带 __media__（与单镜生成一致）
并行调度（D-18）：
- _compute_concurrency：Mock 模式 1；多实例 = 实例数
"""
import json
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
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


def _mk_char_scene_assets(client, n_chars=1, with_scene=True):
    """构造项目 + n 个角色（有定妆图）+ 1 个场景（有基准图），返回 (pid, char_names, scene_name)。"""
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, ids
    pid = client.post("/api/projects", json={"name": "多角色项目", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    char_names = []
    for i in range(n_chars):
        a = Asset(id=ids.asset_id(), project_id=pid, asset_type="character",
                  name=f"p{i}", version=1, file_path=f"/tmp/p{i}.png", validity="current")
        db.add(a)
        db.flush()
        name = f"角色{i}"
        db.add(Character(id=ids.character_id(), project_id=pid, name=name,
                         appearance="x", portrait_asset_id=a.id))
        char_names.append(name)
    scene_name = None
    if with_scene:
        a2 = Asset(id=ids.asset_id(), project_id=pid, asset_type="scene",
                   name="s0", version=1, file_path="/tmp/s0.png", validity="current")
        db.add(a2)
        db.flush()
        scene_name = "教室"
        db.add(Scene(id=ids.scene_id(), project_id=pid, title=scene_name, seq=1,
                     baseline_asset_id=a2.id))
    db.commit()
    db.close()
    return pid, char_names, scene_name


# ---------- 多角色槽位注入 ----------

def test_multi_character_slot_priority(client: TestClient):
    """多角色按出现顺序填入 ref_identity → ref_costume（ref_image 为分镜图保留槽位）。"""
    from app.core.database import SessionLocal
    from app.services import ref_inject
    pid, names, _ = _mk_char_scene_assets(client, n_chars=2)
    db = SessionLocal()
    prompt = f"@{names[0]} 和 @{names[1]} 在对话"
    media = ref_inject.build_media_params(db, pid, prompt, None)
    db.close()
    assert media == {
        "ref_identity": "/tmp/p0.png",
        "ref_costume": "/tmp/p1.png",
    }


def test_character_never_occupies_ref_image(client: TestClient):
    """P0 回归：角色引用不得占用 ref_image（当前分镜图槽位），避免分镜图被覆盖。"""
    from app.core.database import SessionLocal
    from app.services import ref_inject
    pid, names, _ = _mk_char_scene_assets(client, n_chars=1)
    db = SessionLocal()
    # 工作流声明 ref_identity 与 ref_image 均可用
    media = ref_inject.build_media_params(db, pid, f"@{names[0]} 独白", ["ref_identity", "ref_image"])
    db.close()
    assert "ref_image" not in media
    assert media == {"ref_identity": "/tmp/p0.png"}


def test_character_slot_overflow_with_ref_image_declared(client: TestClient):
    """3 个角色仅 2 个角色槽位（ref_image 不计数）时返回 422，不静默丢弃参考。"""
    from app.core.database import SessionLocal
    from app.services import ref_inject
    pid, names, _ = _mk_char_scene_assets(client, n_chars=3)
    db = SessionLocal()
    prompt = f"@{names[0]} 和 @{names[1]} 与 @{names[2]}"
    with pytest.raises(HTTPException) as ei:
        ref_inject.build_media_params(db, pid, prompt, ["ref_identity", "ref_image", "ref_costume"])
    assert ei.value.status_code == 422
    db.close()


def test_multi_character_slot_overflow_422(client: TestClient):
    """角色引用数超过工作流声明槽位时返回 422，不静默丢参考。"""
    from app.core.database import SessionLocal
    from app.services import ref_inject
    pid, names, _ = _mk_char_scene_assets(client, n_chars=2)
    db = SessionLocal()
    prompt = f"@{names[0]} 和 @{names[1]}"
    # 工作流仅声明 ref_identity 一个角色槽位
    with pytest.raises(HTTPException) as ei:
        ref_inject.build_media_params(db, pid, prompt, ["ref_identity"])
    assert ei.value.status_code == 422
    db.close()


def test_same_name_character_scene_422(client: TestClient):
    """角色与场景同名（历史数据兜底）返回 422。"""
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, ids
    from app.services import ref_inject
    pid = client.post("/api/projects", json={"name": "同名项目", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    a = Asset(id=ids.asset_id(), project_id=pid, asset_type="character",
              name="x", version=1, file_path="/tmp/x.png", validity="current")
    db.add(a)
    db.flush()
    # 直接落库绕过 API 校验，模拟历史同名数据
    db.add(Character(id=ids.character_id(), project_id=pid, name="同名",
                     appearance="x", portrait_asset_id=a.id))
    db.add(Scene(id=ids.scene_id(), project_id=pid, title="同名", seq=1,
                 baseline_asset_id=a.id))
    db.commit()
    with pytest.raises(HTTPException) as ei:
        ref_inject.resolve_refs(db, pid, "@同名")
    assert ei.value.status_code == 422
    db.close()


# ---------- 同名校验（创建/改名入口） ----------

def test_character_scene_name_collision_409(client: TestClient):
    """创建/改名角色与场景同名返回 409；反向同理。"""
    pid = client.post("/api/projects", json={"name": "校验项目", "aspect_ratio": "9:16"}).json()["id"]
    # 先建场景
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1})
    assert scn.status_code == 201
    # 创建同名角色 → 409
    r = client.post("/api/characters", json={"project_id": pid, "name": "教室", "appearance": "x"})
    assert r.status_code == 409
    # 反向：先建角色，再建同名场景 → 409
    ch = client.post("/api/characters", json={"project_id": pid, "name": "林晓", "appearance": "x"})
    assert ch.status_code == 201
    r = client.post("/api/scenes", json={"project_id": pid, "title": "林晓", "episode": 1})
    assert r.status_code == 409
    # 改名冲突：场景改名撞角色名 → 409
    scn_id = scn.json()["id"]
    r = client.patch(f"/api/scenes/{scn_id}", json={"title": "林晓"})
    assert r.status_code == 409
    # 改名冲突：角色改名撞场景名 → 409
    ch_id = ch.json()["id"]
    r = client.patch(f"/api/characters/{ch_id}", json={"name": "教室"})
    assert r.status_code == 409


# ---------- 批量与重做端点携带参考图 ----------

_REF_WF_DEFINITION = {
    "1": {
        "class_type": "LoadImage",
        "_meta": {"title": "$ref_identity.~image"},
        "inputs": {"image": "placeholder.png"},
    },
    "2": {
        "class_type": "EmptyLatentImage",
        "_meta": {"title": "$~width, $~height"},
        "inputs": {"width": 512, "height": 288, "batch_size": 1},
    },
    "3": {
        "class_type": "SaveImage",
        "_meta": {"title": "$output.cover"},
        "inputs": {"images": ["2", 0]},
    },
}


def _upload_ref_workflow(client, name="ref-wf"):
    caps = {"reference_slots": ["ref_identity", "ref_scene"]}
    wf = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": "image", "capabilities": json.dumps(caps)},
        files={"file": (f"{name}.json", json.dumps(_REF_WF_DEFINITION), "application/json")},
    )
    assert wf.status_code in (200, 201), wf.text
    return wf.json()


def _mk_shots_with_prompt(client, n=3):
    """构造项目 + 场景 + n 个镜头（image_prompt 含 @角色引用）。"""
    pid, names, _ = _mk_char_scene_assets(client, n_chars=1)
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "操场", "episode": 1}).json()
    shot_ids = []
    for i in range(n):
        shot = client.post("/api/shots", json={
            "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
            "image_prompt": f"@{names[0]} 站在操场上",
        }).json()
        shot_ids.append(shot["id"])
    return pid, names, shot_ids


def test_batch_generate_carries_media(client: TestClient):
    """批量生成端点携带 __media__（与单镜生成一致）。"""
    pid, names, shot_ids = _mk_shots_with_prompt(client, n=3)
    wf = _upload_ref_workflow(client, "batch-ref-wf")
    r = client.post(f"/api/projects/{pid}/generate", json={
        "target": "keyframe", "workflow_id": wf["id"], "shot_ids": shot_ids,
    })
    assert r.status_code == 202, r.text
    jobs = r.json()["jobs"]
    assert len(jobs) == 3
    for j in jobs:
        params = (j.get("effective_inputs") or {}).get("params") or {}
        assert params.get("__media__") == {"ref_identity": "/tmp/p0.png"}, j


def test_regenerate_carries_media(client: TestClient):
    """regenerate 端点携带 __media__。"""
    pid, names, shot_ids = _mk_shots_with_prompt(client, n=3)
    wf = _upload_ref_workflow(client, "regen-ref-wf")
    r = client.post(f"/api/shots/{shot_ids[0]}/regenerate", json={
        "target": "keyframe", "workflow_id": wf["id"],
        "params": {"prompt": f"@{names[0]} 特写"},
    })
    assert r.status_code == 202, r.text
    j = r.json()
    params = (j.get("effective_inputs") or {}).get("params") or {}
    assert params.get("__media__") == {"ref_identity": "/tmp/p0.png"}


# ---------- 并行调度（D-18） ----------

def test_compute_concurrency(client: TestClient, monkeypatch):
    """Mock 模式并发 1；多实例并发 = 实例数。"""
    from app.core.config import Settings
    from app.worker import scheduler
    import app.worker.scheduler as sched_mod
    # Mock 模式 → 1
    monkeypatch.setattr(sched_mod, "get_settings", lambda: Settings(render_mock=True))
    assert scheduler._compute_concurrency() == 1
    # 2 个启用实例 → 2
    monkeypatch.setattr(sched_mod, "get_settings", lambda: Settings(render_mock=False))
    monkeypatch.setattr(
        "app.integrations.comfyui_pool.enabled_instances",
        lambda: [
            {"base_url": "http://a:8188", "weight": 1, "enabled": True},
            {"base_url": "http://b:8188", "weight": 1, "enabled": True},
        ],
    )
    assert scheduler._compute_concurrency() == 2
    # 无实例（回退单实例配置）→ 1
    monkeypatch.setattr(
        "app.integrations.comfyui_pool.enabled_instances",
        lambda: [{"base_url": "http://a:8188", "weight": 1, "enabled": True}],
    )
    assert scheduler._compute_concurrency() == 1
