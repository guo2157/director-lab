"""P3：运行环境检查 + 真实测试提交（不伪造通过）。

对应 docs/plan/workflow-dynamic-audit.md D10：解析成功 ≠ 当前实例能执行。
检查项全部来自真实 /object_info；测试提交用 Schema 默认值编译最小 prompt，
必需媒体槽位无法自造时明确拒绝并列出槽位名。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.comfyui import ComfyUIError
from app.main import app
from app.core.database import make_engine
from app.models import Base
from app.services.workflow import runtime_checker as rc
from app.services.workflow.versions import _dependency_snapshot

WF_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"


def _load(name: str) -> tuple[dict, dict]:
    definition = json.loads((WF_DIR / f"{name}.json").read_text(encoding="utf-8"))
    meta_path = WF_DIR / f"{name}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return definition, meta


class FakeClient:
    """可注入的假 ComfyUI：只实现依赖检查与提交所需的接口。"""

    def __init__(self, object_info=None, history=None, object_info_error=None):
        self.base_url = "http://fake-comfy"
        self._object_info = object_info or {}
        self._history = history
        self._object_info_error = object_info_error
        self.submitted: list[dict] = []

    def _request(self, method, path, **kwargs):
        if path == "/object_info":
            if self._object_info_error:
                raise ComfyUIError("comfy_unreachable", self._object_info_error)
            return self._object_info
        raise ComfyUIError("comfy_execution_error", f"unexpected {path}")

    def submit_prompt(self, workflow, client_id="director-lab"):
        self.submitted.append(workflow)
        return "pid-test-1"

    def get_history(self, prompt_id):
        return self._history

    def get_queue(self):
        return {"queue_running": [], "queue_pending": []}

    def is_job_done(self, history):
        return True, None

    def extract_outputs(self, history):
        return {"1": [{"kind": "image", "filename": "test_00001_.png",
                       "subfolder": "", "type": "output"}]}


def _object_info_for(definition: dict, extra_files: list[str] | None = None) -> dict:
    """构造一个「节点与模型都齐全」的 object_info。"""
    snapshot = _dependency_snapshot(definition)
    info: dict = {ctype: {"input": {"required": {}}} for ctype in snapshot["node_types"]}
    files = list(snapshot["model_files"]) + list(extra_files or [])
    if files:
        info["CheckpointLoaderSimple"] = {"input": {"required": {"ckpt_name": [files, {}]}}}
    return info


# --- 依赖检查 -----------------------------------------------------------

def test_check_ready_when_nodes_and_models_present():
    definition, meta = _load("wf_video_h3_ref2va_std_5s")
    client = FakeClient(object_info=_object_info_for(definition))
    result = rc.check_definition(definition, meta["capabilities"], client=client)
    assert result["runtime_status"] == "ready"
    assert result["missing_node_types"] == []
    assert result["missing_models"] == []
    assert result["required_nodes"] > 5


def test_check_reports_missing_node_types():
    definition, _ = _load("wf_video_h3_ref2va_std_5s")
    info = _object_info_for(definition)
    info.pop("MiniMaxH3ReferenceToVideo")  # 实例没装 H3 原生节点
    result = rc.check_definition(definition, None, client=FakeClient(object_info=info))
    assert result["runtime_status"] == "missing_deps"
    assert result["missing_node_types"] == ["MiniMaxH3ReferenceToVideo"]


def test_check_reports_missing_model_file():
    definition, _ = _load("wf_video_h3_ref2va_std_5s")
    snapshot = _dependency_snapshot(definition)
    info = _object_info_for(definition)
    dropped = snapshot["model_files"][0]
    # 让实例只报出除该文件以外的候选清单
    info["CheckpointLoaderSimple"] = {
        "input": {"required": {"ckpt_name": [[f for f in snapshot["model_files"] if f != dropped], {}]}}
    }
    result = rc.check_definition(definition, None, client=FakeClient(object_info=info))
    assert result["runtime_status"] == "missing_deps"
    assert dropped in result["missing_models"]


def test_check_unreachable_instance_is_not_reported_as_broken_workflow():
    definition, _ = _load("wf_image_t2i_qwen21_turbo")
    client = FakeClient(object_info_error="连接失败: 隧道不可达")
    result = rc.check_definition(definition, None, client=client)
    assert result["runtime_status"] == "unreachable"
    assert "隧道不可达" in result["error"]


# --- 测试提交 -----------------------------------------------------------

def test_submit_test_prunes_optional_media_and_injects_defaults():
    definition, meta = _load("wf_video_h3_ref2va_std_5s")
    client = FakeClient(object_info=_object_info_for(definition))
    result = rc.submit_test(definition, meta["capabilities"], trigger=meta["trigger"], client=client)
    assert result["submitted"] is True
    assert result["prompt_id"] == "pid-test-1"
    assert len(result["pruned_slots"]) == 12  # 9 图 + 3 音频全部未注入，剔除
    assert "width" in result["injected_params"]
    prompt = client.submitted[0]
    # 被剔除的 LoadImage/LoadAudio 节点不再出现在提交的 prompt 里
    assert not any(n.get("class_type") in ("LoadImage", "LoadAudio") for n in prompt.values())


def test_submit_test_refuses_when_required_media_missing():
    definition, meta = _load("wf_video_h3_flf2v")  # $first_frame.~image! 为必填媒体
    result = rc.submit_test(definition, meta["capabilities"], trigger=meta.get("trigger"),
                            client=FakeClient())
    assert result["submitted"] is False
    assert result["reason"] == "workflow_test_requires_media"
    assert "first_frame" in result["blocking_slots"]


def test_test_status_maps_history_to_artifacts():
    result = rc.test_status("pid-test-1", client=FakeClient(history={"outputs": {}}))
    assert result["status"] == "succeeded"
    assert result["artifact_count"] == 1
    assert result["artifacts"]["1"] == ["test_00001_.png"]


# --- API 端点 -----------------------------------------------------------

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


def _upload(client: TestClient, name: str, wf_type: str = "video"):
    definition, meta = _load(name)
    resp = client.post(
        "/api/workflows",
        data={"name": f"运行检查-{name}", "workflow_type": wf_type,
              "trigger": json.dumps(meta.get("trigger")),
              "capabilities": json.dumps(meta.get("capabilities"))},
        files={"file": (f"{name}.json", json.dumps(definition).encode("utf-8"), "application/json")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), definition


def test_endpoint_check_runtime_persists_status(client: TestClient, monkeypatch):
    wf, definition = _upload(client, "wf_video_h3_ref2va_std_5s")
    fake = FakeClient(object_info=_object_info_for(definition))
    monkeypatch.setattr(rc, "ComfyUIClient", lambda base_url=None: fake)

    resp = client.post(f"/api/workflows/{wf['id']}/check-runtime")
    assert resp.status_code == 200, resp.text
    assert resp.json()["runtime_status"] == "ready"
    assert client.get(f"/api/workflows/{wf['id']}").json()["runtime_status"] == "ready"


def test_endpoint_test_returns_202_with_prompt_id(client: TestClient, monkeypatch):
    wf, definition = _upload(client, "wf_video_h3_ref2va_std_5s")
    fake = FakeClient(object_info=_object_info_for(definition))
    monkeypatch.setattr(rc, "ComfyUIClient", lambda base_url=None: fake)

    resp = client.post(f"/api/workflows/{wf['id']}/test")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["prompt_id"] == "pid-test-1"
    assert body["submitted"] is True


def test_endpoint_test_refuses_required_media(client: TestClient, monkeypatch):
    wf, definition = _upload(client, "wf_video_h3_flf2v")
    monkeypatch.setattr(rc, "ComfyUIClient", lambda base_url=None: FakeClient())
    resp = client.post(f"/api/workflows/{wf['id']}/test")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "workflow_test_requires_media"
    assert "first_frame" in detail["blocking_slots"]
