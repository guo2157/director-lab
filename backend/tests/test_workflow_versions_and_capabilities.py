"""P2：不可变工作流版本 + 归一化能力 + 三模态 Schema 分组测试。

对应 docs/plan/workflow-dynamic-audit.md：
- D3 版本不可变（PATCH 不再原地覆盖，历史任务可复现）
- D5 能力不许按名称推断（CapabilityRegistry 从真实标记读）
- D11 槽位/模态语义统一（image / audio / video 分组）
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
from app.models import Base
from app.services.workflow.capabilities import capability_matches, normalize

WF_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"


def _load(name: str) -> tuple[dict, dict]:
    definition = json.loads((WF_DIR / f"{name}.json").read_text(encoding="utf-8"))
    # 自动生成能力与触发条件（不再依赖 .meta.json）
    caps = normalize("unknown", definition, capabilities=None, trigger=None)
    wf_type = caps.get("workflow_type", "unknown")
    _trigger_targets = {"image": "keyframe", "video": "clip"}
    trigger = {"target": _trigger_targets.get(wf_type, wf_type), "required_params": [], "priority": 10}
    meta = {"capabilities": caps, "trigger": trigger, "workflow_type": wf_type}
    return definition, meta


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def _upload(client: TestClient, name: str, wf_type: str = "image", **extra):
    definition, meta = _load(name)
    data = {
        "name": f"版本测试-{name}",
        "workflow_type": wf_type,
        "trigger": json.dumps(meta.get("trigger")),
        "capabilities": json.dumps(meta.get("capabilities")),
    }
    data.update(extra)
    files = {"file": (f"{name}.json", json.dumps(definition).encode("utf-8"), "application/json")}
    resp = client.post("/api/workflows", data=data, files=files)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- 版本不可变 ---------------------------------------------------------

def test_upload_creates_published_v1(client: TestClient):
    wf = _upload(client, "wf_image_t2i_qwen21_turbo")
    assert wf["version"] == 1
    assert wf["version_count"] == 1
    assert wf["status"] == "published"
    assert wf["current_version_id"]

    versions = client.get(f"/api/workflows/{wf['id']}/versions").json()
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["is_current"] is True
    assert versions[0]["changelog"] == "上传导入"


def test_patch_definition_creates_new_version_and_keeps_old_one_immutable(client: TestClient):
    definition, _ = _load("wf_image_t2i_qwen21_turbo")
    wf = _upload(client, "wf_image_t2i_qwen21_turbo")
    v1 = client.get(f"/api/workflows/{wf['id']}/versions").json()[0]
    v1_def_hash = v1["file_hash"]

    # 改动一个节点输入值，制造新定义
    mutated = json.loads(json.dumps(definition))
    for node in mutated.values():
        inputs = node.get("inputs") or {}
        if isinstance(inputs.get("width"), int):
            inputs["width"] = 768
            break
    resp = client.patch(f"/api/workflows/{wf['id']}", json={"definition": mutated})
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["version"] == 2
    assert updated["version_count"] == 2

    versions = {v["version"]: v for v in client.get(f"/api/workflows/{wf['id']}/versions").json()}
    # 旧版本内容与指纹保持不变（不可变）
    assert versions[1]["file_hash"] == v1_def_hash
    assert versions[1]["is_current"] is False
    assert versions[2]["is_current"] is True
    # 历史版本 Schema 仍可读取
    old_schema = client.get(f"/api/workflows/{wf['id']}/schema", params={"version": 1}).json()
    assert old_schema["version"] == 1
    assert old_schema["params"]


def test_rollback_points_to_old_version_without_overwriting(client: TestClient):
    definition, _ = _load("wf_image_t2i_qwen21_turbo")
    wf = _upload(client, "wf_image_t2i_qwen21_turbo")
    wf_id = wf["id"]
    v1_id = client.get(f"/api/workflows/{wf_id}/versions").json()[0]["id"]

    mutated = json.loads(json.dumps(definition))
    for node in mutated.values():
        inputs = node.get("inputs") or {}
        if isinstance(inputs.get("height"), int):
            inputs["height"] = 768
            break
    client.patch(f"/api/workflows/{wf_id}", json={"definition": mutated})
    v2_before = {v["version"]: v for v in client.get(f"/api/workflows/{wf_id}/versions").json()}[2]

    rolled = client.post(f"/api/workflows/{wf_id}/versions/{v1_id}/rollback").json()
    assert rolled["workflow"]["version"] == 1
    assert rolled["workflow"]["current_version_id"] == v1_id

    versions = {v["version"]: v for v in client.get(f"/api/workflows/{wf_id}/versions").json()}
    assert set(versions) == {1, 2}
    assert versions[2]["file_hash"] == v2_before["file_hash"]  # v2 未被覆盖
    assert versions[1]["is_current"] is True


def test_draft_version_is_not_published_until_requested(client: TestClient):
    definition, _ = _load("wf_image_t2i_qwen21_turbo")
    wf = _upload(client, "wf_image_t2i_qwen21_turbo")
    wf_id = wf["id"]
    draft = client.post(f"/api/workflows/{wf_id}/versions",
                        json={"definition": definition, "changelog": "草稿", "publish": False})
    assert draft.status_code == 201, draft.text
    assert draft.json()["is_current"] is False
    assert client.get(f"/api/workflows/{wf_id}").json()["version"] == 1

    published = client.post(
        f"/api/workflows/{wf_id}/versions/{draft.json()['id']}/publish")
    assert published.status_code == 200
    assert published.json()["workflow"]["version"] == 2


def test_dependency_snapshot_records_node_types_and_models(client: TestClient):
    wf = _upload(client, "wf_video_h3_ref2va_std_5s", wf_type="video")
    versions = client.get(f"/api/workflows/{wf['id']}/versions").json()
    snapshot = versions[0]["dependency_snapshot"]
    assert "MiniMaxH3ReferenceToVideo" in snapshot["node_types"]
    assert "LoadAudio" in snapshot["node_types"]
    assert any(f.endswith(".safetensors") for f in snapshot["model_files"])


# --- 能力归一化 ---------------------------------------------------------

def test_h3_capabilities_expose_tts_audio_input_without_name_inference():
    definition, meta = _load("wf_video_h3_ref2va_std_5s")
    caps = normalize("video", definition)
    assert caps["workflow_type"] == "video"
    assert caps["modalities"]["audio_input"] is True
    assert caps["modalities"]["image_input"] is True
    assert caps["modalities"]["video_output"] is True
    assert caps["audio"]["accepts_tts_reference_audio"] is True
    assert caps["audio"]["max_audio_inputs"] == 3
    assert caps["audio"]["supports_native_audio"] is True
    assert caps["video"]["supports_previous_tail_frame"] is True
    assert caps["video"]["supports_first_frame"] is False
    assert caps["reference"]["max_image_references"] == 9
    assert set(caps["generation_modes"]) >= {
        "text_to_video", "image_to_video", "reference_to_video", "audio_conditioned_video",
    }
    # 音频槽位语义 = 本镜台词语音（voice_audio），不是音色参考
    audio_slot_semantics = {s["semantic"] for s in caps["reference"]["slots"] if s["media_type"] == "audio"}
    assert audio_slot_semantics == {"voice_audio"}


def test_t2i_capabilities_have_no_audio_input():
    definition, meta = _load("wf_image_t2i_qwen21_turbo")
    caps = normalize("image", definition)
    assert caps["modalities"]["audio_input"] is False
    assert caps["audio"]["accepts_tts_reference_audio"] is False
    assert caps["modalities"]["image_output"] is True
    assert "text_to_image" in caps["generation_modes"]


def test_capability_matches_reports_missing_paths():
    t2i_def, t2i_meta = _load("wf_image_t2i_qwen21_turbo")
    t2i = normalize("image", t2i_def)
    ok, missing = capability_matches(t2i, {"modalities.audio_input": True})
    assert ok is False
    assert missing == ["modalities.audio_input"]
    ok2, missing2 = capability_matches(t2i, {"modalities.image_output": True})
    assert ok2 is True and missing2 == []


# --- 三模态 Schema 分组 -------------------------------------------------

def test_schema_groups_reference_audio_and_parameters_separately(client: TestClient):
    wf = _upload(client, "wf_video_h3_ref2va_std_5s", wf_type="video")
    schema = client.get(f"/api/workflows/{wf['id']}/schema").json()
    assert len(schema["reference_inputs"]) == 9
    assert len(schema["audio_inputs"]) == 3
    assert schema["video_inputs"] == []
    param_keys = {p["key"] for p in schema["parameters"]}
    assert {"prompt", "length", "width", "height", "seed"} <= param_keys
    assert not ({"ref_identity", "ref_audio_0"} & param_keys)
    # 全量平铺数组保留给旧消费方
    assert len(schema["params"]) == len(schema["parameters"]) + 12
    assert all("binding" not in p for p in schema["params"])
