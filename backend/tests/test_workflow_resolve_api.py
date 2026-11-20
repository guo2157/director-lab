"""G1/G2 的 HTTP 层契约：/api/workflows/resolve + 项目工作流配置端点。

与 test_workflow_matcher.py 的分工：
- matcher 单测覆盖决策逻辑（纯 service 层）
- 本文件覆盖真实路由：状态码、detail.code、候选可用性、项目默认优先级

全部数据来自真实工作流 JSON（`backend/data/workflows/`）。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, ids
from app.models.narrative import Project
from app.models.production import Workflow

WF_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"
# 已退役的工作流定义（数据库记录已删除、seed 不再导入）仍保留 JSON 作为
# 解析/分类逻辑的测试样本，放在 _retired/ 下；读取时回退到这里。
WF_RETIRED_DIR = WF_DIR / "_retired"


def wf_path(name: str) -> Path:
    """工作流 JSON 路径：优先在用目录，其次已退役目录。"""
    p = WF_DIR / f"{name}.json"
    return p if p.exists() else WF_RETIRED_DIR / f"{name}.json"


def _load(name: str) -> tuple[dict, dict]:
    definition = json.loads(wf_path(name).read_text(encoding="utf-8"))
    meta_path = WF_DIR / f"{name}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return definition, meta


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    with TestClient(app) as c:
        yield c


def _seed(client: TestClient) -> dict[str, str]:
    """通过真实上传端点导入 3 份工作流，返回 name → id。"""
    out: dict[str, str] = {}
    for name, wtype in (
        ("wf_image_t2i_qwen21_turbo", "image"),
        ("wf_image_ref_qwen21_multi", "image"),
        ("wf_video_h3_ref2va_std_5s", "video"),
        ("wf_video_h3_t2v", "video"),
    ):
        resp = client.post(
            "/api/workflows",
            data={"name": name, "workflow_type": wtype},
            files={"file": (f"{name}.json", wf_path(name).read_bytes(), "application/json")},
        )
        assert resp.status_code == 201, resp.text
        out[name] = resp.json()["id"]
    return out


def _create_project(client: TestClient) -> str:
    resp = client.post("/api/projects", json={"name": "工作流配置测试", "aspect_ratio": "9:16"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


# --- 项目工作流配置端点 ---------------------------------------------------

def test_get_workflow_config_lists_candidates_by_slot(client):
    _seed(client)
    pid = _create_project(client)
    resp = client.get(f"/api/projects/{pid}/workflow-config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["defaults"] == {}
    assert set(data["slots"]) == {"image", "storyboard_image", "video", "tts", "bgm", "sfx"}
    # 两个 image 工作流都进入 image 槽位候选
    image_ids = {c["id"] for c in data["candidates"]["image"]}
    assert len(image_ids) == 2
    # 每候选都带 usable 与能力摘要；含参考槽位的工作流标记为不可用（资产纯度规则）
    usable_count = 0
    for cand in data["candidates"]["image"]:
        assert "usable" in cand and "modalities" in cand
        if cand["usable"]:
            usable_count += 1
    assert usable_count == 1, "仅纯净文生图工作流（无 reference_slots）应为 usable"


def test_put_workflow_config_saves_and_roundtrips(client):
    wfs = _seed(client)
    pid = _create_project(client)
    resp = client.put(
        f"/api/projects/{pid}/workflow-config",
        json={"defaults": {"video": wfs["wf_video_h3_ref2va_std_5s"],
                           "storyboard_image": wfs["wf_image_ref_qwen21_multi"]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["defaults"]["video"] == wfs["wf_video_h3_ref2va_std_5s"]
    got = client.get(f"/api/projects/{pid}/workflow-config").json()
    assert got["defaults"]["storyboard_image"] == wfs["wf_image_ref_qwen21_multi"]


def test_put_workflow_config_rejects_wrong_type(client):
    wfs = _seed(client)
    pid = _create_project(client)
    resp = client.put(
        f"/api/projects/{pid}/workflow-config",
        json={"defaults": {"video": wfs["wf_image_t2i_qwen21_turbo"]}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


def test_put_workflow_config_rejects_unknown_slot(client):
    wfs = _seed(client)
    pid = _create_project(client)
    resp = client.put(
        f"/api/projects/{pid}/workflow-config",
        json={"defaults": {"hologram": wfs["wf_image_t2i_qwen21_turbo"]}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"


def test_put_workflow_config_rejects_missing_workflow(client):
    pid = _create_project(client)
    resp = client.put(
        f"/api/projects/{pid}/workflow-config",
        json={"defaults": {"video": "wf_ghost"}},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "workflow_not_found"


def test_put_empty_value_clears_slot(client):
    wfs = _seed(client)
    pid = _create_project(client)
    client.put(f"/api/projects/{pid}/workflow-config",
               json={"defaults": {"video": wfs["wf_video_h3_ref2va_std_5s"]}})
    resp = client.put(f"/api/projects/{pid}/workflow-config", json={"defaults": {"video": ""}})
    assert resp.status_code == 200
    assert resp.json()["defaults"] == {}


# --- /api/workflows/resolve 契约 -----------------------------------------

def test_resolve_endpoint_returns_workflow_and_source(client):
    wfs = _seed(client)
    resp = client.post("/api/workflows/resolve", json={"slot": "video"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workflow"]["id"] in (wfs["wf_video_h3_ref2va_std_5s"], wfs["wf_video_h3_t2v"])
    assert body["source"] in ("capability_match", "system_default")


def test_resolve_endpoint_honours_project_default(client):
    wfs = _seed(client)
    pid = _create_project(client)
    client.put(f"/api/projects/{pid}/workflow-config",
               json={"defaults": {"video": wfs["wf_video_h3_t2v"]}})
    resp = client.post("/api/workflows/resolve", json={"slot": "video", "project_id": pid})
    assert resp.status_code == 200, resp.text
    assert resp.json()["workflow"]["id"] == wfs["wf_video_h3_t2v"]
    assert resp.json()["source"] == "project_default"


def test_resolve_endpoint_explicit_incompatible_returns_422_with_missing(client):
    """显式指定不支持音频的工作流 → 422 + 缺失能力列表，不回退。"""
    wfs = _seed(client)
    resp = client.post(
        "/api/workflows/resolve",
        json={"slot": "video", "workflow_id": wfs["wf_video_h3_t2v"],
              "required": {"modalities.audio_input": True}},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "workflow_capability_mismatch"
    assert "modalities.audio_input" in detail["missing_capabilities"]


def test_resolve_endpoint_no_candidate_reports_missing(client):
    _seed(client)  # 只有 image/video，没有 tts
    resp = client.post("/api/workflows/resolve", json={"slot": "tts"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "workflow_capability_mismatch"
    assert detail["slot"] == "tts"


def test_resolve_endpoint_rejects_unknown_slot(client):
    resp = client.post("/api/workflows/resolve", json={"slot": "hologram"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
