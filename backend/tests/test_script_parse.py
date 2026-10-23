"""阶段 4 剧本解析链路集成测试：script-versions / estimate / confirm / parse / parse-version confirm / jobs。
语言模型调用用 monkeypatch 替换为替身，不依赖外部服务。
"""
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
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    # 执行器内部直接引用了 SessionLocal，同步替换
    import app.worker.parse_executor as pe_mod
    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    return TestClient(app)


SCRIPT_TEXT = "第一集。林晓走进教室，对同桌说：\"你看见我的笔记本了吗？\" 同桌摇头。窗外下起了雨。" * 30


def _make_project(client: TestClient) -> str:
    r = client.post("/api/projects", json={"name": "解析测试项目", "aspect_ratio": "9:16"})
    assert r.status_code == 201
    return r.json()["id"]


def _save_script(client: TestClient, pid: str, text: str = SCRIPT_TEXT) -> dict:
    r = client.post(f"/api/projects/{pid}/script-versions", json={"text": text})
    assert r.status_code == 201
    return r.json()


def _confirmed_estimate(client: TestClient, pid: str, sv: dict) -> None:
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT})
    assert r.status_code == 201
    est_id = r.json()["estimate_id"]
    r = client.post(f"/api/projects/{pid}/estimates/{est_id}/confirm",
                    json={"script_version_id": sv["script_version_id"],
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 200


FAKE_PARSE_OUTPUT = {
    "project_analysis": {"summary": "教室悬疑", "theme": "信任", "highlights": ["反转"],
                         "foreshadowing": []},
    "characters": [{"name": "林晓", "description": "转学生", "traits": "细心", "appearance": "短发"}],
    "props": [{"name": "笔记本", "description": "蓝色封面", "appearance": "磨损"}],
    "scenes": [{
        "episode": 1, "title": "教室", "description": "午后教室", "time_note": "白天",
        "shots": [
            {"episode": 1, "seq": 1, "scene_id": "教室", "character_id": "林晓",
             "dialogue": "你看见我的笔记本了吗？", "duration": 5,
             "image_prompt": "教室里的 @林晓", "shot_kind": "dialogue"},
            {"episode": 1, "seq": 2, "scene_id": "教室",
             "duration": 4, "image_prompt": "窗外的雨"},
        ],
    }],
    "episodes": [{"episode": 1, "goal": "建立悬念", "rhythm_curve": "平稳",
                  "cliffhanger_type": "信息缺失", "hook_to_next": "笔记本下落", "locked": False}],
}


def test_script_version_immutable(client):
    pid = _make_project(client)
    sv1 = _save_script(client, pid)
    assert sv1["version"] == 1
    sv2 = _save_script(client, pid, SCRIPT_TEXT + "补充内容。")
    assert sv2["version"] == 2
    assert sv2["content_hash"] != sv1["content_hash"]


def test_estimate_hash_validation(client):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    # 文本与版本哈希不符 → 409 script_version_conflict
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": "完全不同的文本"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "script_version_conflict"
    # 正常评估
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT})
    assert r.status_code == 201
    body = r.json()
    assert body["char_count"] == len(SCRIPT_TEXT)
    assert body["estimated_episodes"] >= 1
    assert body["user_confirmed"] is False
    assert len(body["episode_breakdown"]) == body["estimated_episodes"]


def test_parse_requires_confirmed_estimate(client):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    # 未评估直接解析 → 400
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 400
    # 评估但未确认 → 仍 400
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT})
    assert r.status_code == 201
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 400


def test_parse_job_lifecycle(client, monkeypatch):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv)
    # 提交解析 → 202 + job_id，且项目自动激活
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["status"] == "active"
    # 任务详情可读，状态为 queued
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "queued"
    assert job["job_type"] == "parse"
    # 用替身替换语言模型后执行解析
    import app.worker.parse_executor as pe_mod

    class _FakeLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, system_prompt, user_prompt):
            return FAKE_PARSE_OUTPUT

    monkeypatch.setattr(pe_mod, "LLMClient", _FakeLLM)
    result = pe_mod.execute_parse_job(job_id)
    assert result["ok"] is True
    assert result["shots"] == 2
    # 任务终态与结果
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "succeeded"
    assert job["result"]["parse_version_id"]
    assert len(job["result"]["scenes"]) == 1
    # 归一化：第 2 镜缺 shot_kind 按 visual 补齐并记警告
    shot2 = job["result"]["scenes"][0]["shots"][1]
    assert shot2["shot_kind"] == "visual"
    assert any("shot_kind" in w for w in job["result"]["parse_warnings"])
    # 归一化：第 1 镜 emotion 缺失落 neutral
    shot1 = job["result"]["scenes"][0]["shots"][0]
    assert shot1["emotion"] == "neutral"


def test_parse_version_confirm_optimistic_lock(client, monkeypatch):
    # 走完整流程拿到 parse_version_id
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv)
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
    pe_mod.execute_parse_job(job_id)
    pv_id = client.get(f"/api/jobs/{job_id}").json()["result"]["parse_version_id"]
    # 乐观锁：携带错误的当前版本 → 409
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv_id}/confirm",
                    json={"current_parse_version_id": "pv_not_exist"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "parse_version_conflict"
    # 正确确认（当前为空）
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv_id}/confirm",
                    json={"current_parse_version_id": None})
    assert r.status_code == 200
    assert r.json()["status"] == "current"
    # 重复确认 → 409（已非 candidate）
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv_id}/confirm",
                    json={"current_parse_version_id": pv_id})
    assert r.status_code == 409


FAKE_PARSE_OUTPUT_V2 = {
    "project_analysis": {"summary": "教室悬疑v2", "theme": "信任", "highlights": [], "foreshadowing": []},
    "characters": [{"name": "林晓", "description": "转学生（更新）", "traits": "细心", "appearance": "短发"}],
    "props": [],
    "scenes": [{
        "episode": 1, "title": "教室", "description": "午后教室", "time_note": "白天",
        "shots": [
            {"episode": 1, "seq": 1, "scene_id": "教室", "character_id": "林晓",
             "dialogue": "新台词", "duration": 6, "shot_kind": "dialogue",
             "image_prompt": "新的 @林晓"},
        ],
    }],
    "episodes": [{"episode": 1, "goal": "新目标", "rhythm_curve": "上扬",
                  "cliffhanger_type": "危机逼近", "hook_to_next": "承接", "locked": False}],
}


def _run_parse(client, monkeypatch, pid, sv, fake_output):
    """提交解析任务并用替身执行，返回 parse_version_id。"""
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    import app.worker.parse_executor as pe_mod

    class _FakeLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, s, u):
            return fake_output

    monkeypatch.setattr(pe_mod, "LLMClient", _FakeLLM)
    res = pe_mod.execute_parse_job(job_id)
    assert res["ok"] is True
    return client.get(f"/api/jobs/{job_id}").json()["result"]["parse_version_id"]


def test_parse_confirm_cascades_entities(client, monkeypatch):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv)
    # 第一次解析并确认：实体落库
    pv1 = _run_parse(client, monkeypatch, pid, sv, FAKE_PARSE_OUTPUT)
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv1}/confirm",
                    json={"current_parse_version_id": None})
    assert r.status_code == 200
    chars = client.get(f"/api/projects/{pid}/characters").json()
    assert [c["name"] for c in chars] == ["林晓"]
    scenes = client.get(f"/api/projects/{pid}/scenes").json()
    assert len(scenes) == 1 and scenes[0]["title"] == "教室"
    scene_id = scenes[0]["id"]
    shots = client.get(f"/api/scenes/{scene_id}/shots?episode=1").json()["items"]
    assert len(shots) == 2
    assert shots[0]["status"] == "pending"
    assert shots[0]["character_id"] == chars[0]["id"]
    plan = client.get(f"/api/projects/{pid}").json()["episode_plan"]
    assert plan[0]["goal"] == "建立悬念"
    # 第二次解析并确认：镜头按范围清理更新，旧版本失效
    pv2 = _run_parse(client, monkeypatch, pid, sv, FAKE_PARSE_OUTPUT_V2)
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv2}/confirm",
                    json={"current_parse_version_id": pv1})
    assert r.status_code == 200
    shots = client.get(f"/api/scenes/{scene_id}/shots?episode=1").json()["items"]
    assert len(shots) == 1  # 旧 seq=2 被清理
    assert shots[0]["seq"] == 1 and shots[0]["dialogue"] == "新台词"
    assert shots[0]["duration"] == 6
    chars = client.get(f"/api/projects/{pid}/characters").json()
    assert len(chars) == 1 and chars[0]["description"] == "转学生（更新）"
    plan = client.get(f"/api/projects/{pid}").json()["episode_plan"]
    assert plan[0]["goal"] == "新目标"
    # 旧解析版本已标记 superseded
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv1}/confirm",
                    json={"current_parse_version_id": pv2})
    assert r.status_code == 409


def test_parse_llm_failure_classification(client, monkeypatch):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv)
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    job_id = r.json()["job_id"]
    import app.worker.parse_executor as pe_mod
    from app.integrations.llm import LLMError

    class _FailLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, s, u):
            raise LLMError("llm_unconfigured", "语言模型未配置 API Key（DLAB_LLM_API_KEY）")

    monkeypatch.setattr(pe_mod, "LLMClient", _FailLLM)
    result = pe_mod.execute_parse_job(job_id)
    assert result["ok"] is False
    assert result["reason"] == "llm_unconfigured"
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error_code"] == "llm_unconfigured"
    assert job["retryable"] is True
    # 当前分镜不受影响
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["current_parse_version_id"] is None
