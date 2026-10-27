"""提示词模板整改（P4）测试。
覆盖：
- 模板必填变量校验：名称/外观/描述全空时明确报错，不静默生成占位提示词
- 负面约束按工作流实际参数绑定（$negative.*）注入，不再依赖未声明的 capabilities.negative_prompt
- 未注入媒体的参考图槽位在提交前剔除节点及连线（占位图不再污染生成结果）
- 资产图提示词模板版本写入 input_snapshot 可追溯
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


@pytest.fixture()
def mock_client(tmp_path, monkeypatch):
    """开启 Mock 渲染的客户端（用于执行 executor 全链路）。"""
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    import app.core.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: cfg_mod.Settings(render_mock=True, media_dir=tmp_path / "media"))
    import app.worker.executor as ex_mod
    monkeypatch.setattr(ex_mod, "get_settings", lambda: cfg_mod.Settings(render_mock=True, media_dir=tmp_path / "media"))
    return TestClient(app)


# ---------- 模板必填变量校验 ----------

def test_character_prompt_missing_identity_raises():
    from app.services.asset_prompts import build_character_prompt
    with pytest.raises(ValueError, match="missing_identity"):
        build_character_prompt("", "", description="")


def test_scene_prompt_missing_identity_raises():
    from app.services.asset_prompts import build_scene_prompt
    with pytest.raises(ValueError, match="missing_identity"):
        build_scene_prompt("", "")


def test_prop_prompt_missing_identity_raises():
    from app.services.asset_prompts import build_prop_prompt
    with pytest.raises(ValueError, match="missing_identity"):
        build_prop_prompt("", "", description="")


def test_api_accepts_name_only_character(client: TestClient):
    """仅有名称的角色：名称作为身份兜底正常生成（不虚构外观），不报错。"""
    pid = client.post("/api/projects", json={"name": "p4a", "aspect_ratio": "9:16"}).json()["id"]
    ch = client.post("/api/characters", json={"project_id": pid, "name": "空角色"}).json()
    wf_id = _create_wf(client, "img-empty", _WF_WITHOUT_NEGATIVE)
    r = client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    params = r.json()["effective_inputs"]["params"]
    # 名称作为身份兜底，不虚构外观描述
    assert "角色定义：空角色" in params["prompt"]
    assert params.get("__asset_prompt_version__") == "2.0"


# ---------- 负面约束按实际参数绑定注入 ----------

_WF_WITH_NEGATIVE = {
    "1": {"class_type": "EmptyLatentImage", "_meta": {"title": "$~width, $~height"},
          "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "_meta": {"title": "$prompt.text!"},
          "inputs": {"clip": ["2", 0], "text": "a portrait photo"}},
    "7": {"class_type": "CLIPTextEncode", "_meta": {"title": "$negative.text"},
          "inputs": {"clip": ["2", 0], "text": ""}},
    "9": {"class_type": "SaveImage", "_meta": {"title": "$output.cover"},
          "inputs": {"images": ["1", 0]}},
}

_WF_WITHOUT_NEGATIVE = {
    "1": {"class_type": "EmptyLatentImage", "_meta": {"title": "$~width, $~height"},
          "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "_meta": {"title": "$prompt.text!"},
          "inputs": {"clip": ["2", 0], "text": "a portrait photo"}},
    "9": {"class_type": "SaveImage", "_meta": {"title": "$output.cover"},
          "inputs": {"images": ["1", 0]}},
}


def _create_wf(client, name, definition):
    r = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": "image"},
        files={"file": (f"{name}.json", json.dumps(definition), "application/json")},
    )
    assert r.status_code < 400, r.text
    return r.json()["id"]


def test_negative_injected_when_workflow_binds(client: TestClient):
    """工作流绑定 $negative 标记时，角色定妆图任务自动携带负面约束。"""
    pid = client.post("/api/projects", json={"name": "p4b", "aspect_ratio": "9:16"}).json()["id"]
    ch = client.post("/api/characters", json={
        "project_id": pid, "name": "小马", "appearance": "棕黄色毛发的小马",
    }).json()
    wf_id = _create_wf(client, "img-neg", _WF_WITH_NEGATIVE)
    r = client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    job = r.json()
    params = job["effective_inputs"]["params"]
    assert params.get("negative")
    assert "第二个人物" in params["negative"]
    assert params.get("__asset_prompt_version__")


def test_negative_not_injected_without_binding(client: TestClient):
    """工作流未绑定 $negative 标记时不注入，避免写入无人消费的参数。"""
    pid = client.post("/api/projects", json={"name": "p4c", "aspect_ratio": "9:16"}).json()["id"]
    ch = client.post("/api/characters", json={
        "project_id": pid, "name": "小马", "appearance": "棕黄色毛发的小马",
    }).json()
    wf_id = _create_wf(client, "img-noneg", _WF_WITHOUT_NEGATIVE)
    r = client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    params = r.json()["effective_inputs"]["params"]
    assert "negative" not in params
    assert "negative_prompt" not in params


# ---------- 未注入参考图槽位剔除 ----------

def test_prune_unbound_media_inputs():
    """未提供媒体的参考图节点及连线被剔除；已提供的保留。"""
    from app.services.workflow_inject import prune_unbound_media_inputs
    wf = {
        "20": {"class_type": "LoadImage", "_meta": {"title": "$ref_identity.~image"},
               "inputs": {"image": "placeholder_identity.png"}},
        "21": {"class_type": "LoadImage", "_meta": {"title": "$ref_scene.~image"},
               "inputs": {"image": "placeholder_scene.png"}},
        "7": {"class_type": "TextEncode", "_meta": {"title": "$prompt.text!"},
               "inputs": {"clip": ["2", 0], "text": "x", "image1": ["20", 0], "image2": ["21", 0]}},
        "9": {"class_type": "SaveImage", "_meta": {"title": "$output.cover"},
               "inputs": {"images": ["7", 0]}},
    }
    out, pruned = prune_unbound_media_inputs(wf, {"ref_identity": "uploaded.png"})
    assert pruned == ["ref_scene"]
    assert "21" not in out  # 未注入的 LoadImage 节点被删除
    assert "20" in out      # 已注入的保留
    assert "image1" in out["7"]["inputs"]
    assert "image2" not in out["7"]["inputs"]  # 消费连线被剔除


def test_prune_keeps_required_media():
    """必填媒体参数缺失时不剔除，交由 inject_params 报必填参数缺失。"""
    from app.services.workflow_inject import prune_unbound_media_inputs
    wf = {
        "20": {"class_type": "LoadImage", "_meta": {"title": "$ref_identity.~image!"},
               "inputs": {"image": "placeholder_identity.png"}},
        "7": {"class_type": "TextEncode", "_meta": {"title": "$prompt.text!"},
               "inputs": {"clip": ["2", 0], "text": "x", "image1": ["20", 0]}},
        "9": {"class_type": "SaveImage", "_meta": {"title": "$output.cover"},
               "inputs": {"images": ["7", 0]}},
    }
    out, pruned = prune_unbound_media_inputs(wf, {})
    assert pruned == []
    assert "20" in out


# ---------- 模板版本追溯（executor 全链路） ----------

def test_prompt_version_recorded_in_snapshot(mock_client: TestClient):
    """Mock 模式全链路：模板版本持久化到 input_snapshot，且不作为参数提交。"""
    pid = mock_client.post("/api/projects", json={"name": "p4d", "aspect_ratio": "9:16"}).json()["id"]
    ch = mock_client.post("/api/characters", json={
        "project_id": pid, "name": "小马", "appearance": "棕黄色毛发的小马",
    }).json()
    wf_id = _create_wf(mock_client, "img-ver", _WF_WITHOUT_NEGATIVE)
    r = mock_client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    from app.worker.executor import execute_generate_job
    result = execute_generate_job(jid)
    assert result["ok"], result
    from app.core.database import SessionLocal
    from app.models import Job
    db = SessionLocal()
    try:
        job = db.get(Job, jid)
        snap = job.input_snapshot or {}
        assert snap.get("asset_prompt_version") == "2.0"
        # 版本标记不得残留在提交参数中
        assert "__asset_prompt_version__" not in (snap.get("params") or {})
    finally:
        db.close()
