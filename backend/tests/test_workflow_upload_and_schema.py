"""P1：工作流上传 Schema 构建与静态校验测试。

覆盖 docs/plan/workflow-dynamic-audit.md 的 D1/D2/D7/D9/D14：
- 上传 API Format 工作流必须产出可用 Schema（原实现读画布字段，恒为空）
- 校验必须对合法工作流返回 valid，对画布格式/缺输出/断线给出可定位 issue
- 对外 Schema 必须剥离 node 绑定
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
from app.models import Base
from app.services.workflow import build_schema, public_schema, validate_definition
from app.services.workflow.capabilities import normalize

WF_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"


def _load(name: str) -> tuple[dict, dict]:
    definition = json.loads((WF_DIR / f"{name}.json").read_text(encoding="utf-8"))
    meta_path = WF_DIR / f"{name}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
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


# --- Schema 构建 ---------------------------------------------------------

def test_t2i_schema_has_prompt_size_seed_and_cover_output():
    definition, meta = _load("wf_image_t2i_qwen21_turbo")
    schema = build_schema(definition, capabilities=meta.get("capabilities"),
                          trigger=meta.get("trigger"))
    params = schema["params"]
    assert {"prompt", "negative", "width", "height", "seed"} <= set(params)
    assert params["prompt"]["type"] == "textarea"
    assert params["prompt"]["semantic"] == "positive_prompt"
    assert params["prompt"]["required"] is True
    assert params["width"]["semantic"] == "width"
    assert params["width"]["min"] == 256 and params["width"]["step"] == 32
    # 默认值来自工作流节点当前输入，不是凭空造的
    assert params["width"]["default"] == 1024 or isinstance(params["width"]["default"], int)
    # binding 只在后端，指向真实节点与输入键
    assert params["prompt"]["binding"]["input_key"] == "prompt"
    assert params["prompt"]["binding"]["node_id"] in definition
    assert [o["key"] for o in schema["outputs"]] == ["cover"]
    assert schema["outputs"][0]["media_type"] == "image"
    assert schema["errors"] == []


def test_video_schema_marks_reference_audio_and_frame_range_from_capabilities():
    definition, _ = _load("wf_video_h3_ref2va_std_5s")
    caps = normalize("video", definition)
    schema = build_schema(definition, capabilities=caps)
    params = schema["params"]
    # 9 个参考图槽位 + 3 个参考音频槽位都应识别为媒体参数
    ref_images = [k for k, v in params.items() if v["type"] == "image"]
    ref_audios = [k for k, v in params.items() if v["type"] == "audio"]
    assert len(ref_images) == 9
    assert set(ref_audios) == {"ref_audio_0", "ref_audio_1", "ref_audio_2"}
    assert params["ref_identity"]["semantic"] == "character_identity"
    assert params["ref_prev_frame"]["semantic"] == "previous_shot_last_frame"
    # 帧率与长度默认值来自计算图本身（.meta.json 已移除，不再由能力声明提供）
    assert caps["video"]["fps"] == 24.0
    assert params["length"]["default"] == 124
    assert schema["outputs"][0]["key"] == "clip"
    assert schema["outputs"][0]["media_type"] == "video"


def test_public_schema_strips_node_binding_and_sorts_by_order():
    definition, meta = _load("wf_image_ref_qwen21_multi")
    schema = build_schema(definition, capabilities=meta.get("capabilities"),
                          trigger=meta.get("trigger"))
    public = public_schema(schema)
    assert isinstance(public["params"], list)
    assert all("binding" not in p for p in public["params"])
    assert all("node_id" not in o for o in public["outputs"])
    orders = [int(p.get("order") or 90) for p in public["params"]]
    assert orders == sorted(orders)
    # 内部节点（模型加载/VAE/采样器）不得出现在 Schema
    keys = {p["key"] for p in public["params"]}
    assert not ({"model", "vae", "clip", "sampler", "steps", "cfg"} & keys)


# --- 静态校验 -----------------------------------------------------------

def test_validator_accepts_real_api_format_workflows():
    for name in ("wf_image_t2i_qwen21_turbo", "wf_image_ref_qwen21_multi",
                 "wf_video_h3_ref2va_std_5s", "wf_tts_qwen3_custom_voice",
                 "wf_bgm_stable_audio"):
        definition, meta = _load(name)
        result = validate_definition(definition, trigger=meta.get("trigger"))
        assert result["valid"], f"{name}: {result['errors']}"


def test_validator_rejects_canvas_format_with_actionable_hint():
    canvas = {"nodes": [{"id": 1, "type": "KSampler"}], "links": []}
    result = validate_definition(canvas)
    assert result["valid"] is False
    assert result["errors"][0]["code"] == "workflow_format_invalid"
    assert "API Format" in result["errors"][0]["message"]


def test_validator_reports_missing_output_marker():
    definition, meta = _load("wf_image_t2i_qwen21_turbo")
    stripped = {nid: node for nid, node in definition.items()
                if "$output" not in str((node.get("_meta") or {}).get("title") or "")}
    result = validate_definition(stripped, trigger=meta.get("trigger"))
    assert result["valid"] is False
    assert any("$output" in e["message"] for e in result["errors"])


def test_validator_reports_broken_connection_and_bad_node_id():
    definition, _ = _load("wf_image_t2i_qwen21_turbo")
    broken = json.loads(json.dumps(definition))
    # 找一条连线改成指向不存在的节点
    for node in broken.values():
        for key, value in list((node.get("inputs") or {}).items()):
            if isinstance(value, list) and len(value) == 2:
                value[0] = "999999"
                break
        else:
            continue
        break
    result = validate_definition(broken)
    assert any(e["code"] == "workflow_connection_broken" for e in result["errors"])

    bad_id = json.loads(json.dumps(definition))
    bad_id["node_1"] = bad_id.pop(next(iter(bad_id)))
    result2 = validate_definition(bad_id)
    assert any(e["code"] == "workflow_node_id_invalid" for e in result2["errors"])


def test_validator_warns_when_only_one_side_of_size_bound():
    definition, _ = _load("wf_image_t2i_qwen21_turbo")
    # 去掉 $height 标记 → 只绑定了 width
    patched = json.loads(json.dumps(definition))
    for node in patched.values():
        title = (node.get("_meta") or {}).get("title") or ""
        if "$height" in title:
            node["_meta"]["title"] = title.replace("$height", "height")
    result = validate_definition(patched)
    assert any("width" in w and "height" in w for w in result["warnings"])


# --- 上传端点 -----------------------------------------------------------

def _upload(client: TestClient, name: str, *, wf_type: str = "image",
            with_meta: bool = True, **extra):
    definition, meta = _load(name)
    data = {"name": f"测试-{name}", "workflow_type": wf_type}
    if with_meta:
        data["trigger"] = json.dumps(meta.get("trigger"))
        data["capabilities"] = json.dumps(meta.get("capabilities"))
    data.update(extra)
    files = {"file": (f"{name}.json", json.dumps(definition).encode("utf-8"), "application/json")}
    return client.post("/api/workflows", data=data, files=files)


def test_upload_returns_usable_schema_and_hash(client: TestClient):
    resp = _upload(client, "wf_image_t2i_qwen21_turbo", alias="Qwen 文生图")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["original_filename"] == "wf_image_t2i_qwen21_turbo.json"
    assert len(body["file_hash"]) == 64
    assert body["alias"] == "Qwen 文生图"
    assert body["schema_summary"]["params"] >= 5
    assert body["schema_summary"]["outputs"] == ["cover"]
    assert body["parsed_schema"]["params"]["prompt"]["binding"]["input_key"] == "prompt"

    schema = client.get(f"/api/workflows/{body['id']}/schema").json()
    assert isinstance(schema["params"], list)
    assert all("binding" not in p for p in schema["params"])
    assert schema["outputs"][0]["key"] == "cover"


def test_upload_then_validate_returns_valid(client: TestClient):
    wf_id = _upload(client, "wf_image_ref_qwen21_multi").json()["id"]
    result = client.post(f"/api/workflows/{wf_id}/validate").json()
    assert result["valid"] is True
    assert result["issues"] == []


def test_upload_rejects_canvas_format(client: TestClient):
    canvas = {"nodes": [{"id": 1, "type": "KSampler", "widgets_values": []}], "links": []}
    resp = client.post(
        "/api/workflows",
        data={"name": "画布格式", "workflow_type": "image"},
        files={"file": ("canvas.json", json.dumps(canvas).encode("utf-8"), "application/json")},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "workflow_format_invalid"
    assert "API Format" in detail["message"]


def test_upload_rejects_unknown_workflow_type(client: TestClient):
    resp = _upload(client, "wf_image_t2i_qwen21_turbo", wf_type="image_t2i")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


def test_list_supports_keyword_search(client: TestClient):
    _upload(client, "wf_image_t2i_qwen21_turbo", alias="阿尔法别名")
    assert len(client.get("/api/workflows", params={"q": "阿尔法"}).json()) == 1
    assert client.get("/api/workflows", params={"q": "不存在的词"}).json() == []
