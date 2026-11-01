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


FAKE_OUTLINE_OUTPUT = {
    "total_episodes": 1,
    "estimated_total_duration": 90,
    "dialogue_char_estimate": 50,
    "episodes": [
        {"episode": 1, "title": "第一集", "estimated_duration": 90, "estimated_shots": 9},
    ],
}


def _mock_outline(monkeypatch) -> None:
    """mock LLM 大纲分析（estimate 端点依赖 LLM，测试用替身）。"""
    import app.api.v1.script as script_mod
    monkeypatch.setattr(script_mod, "analyze_script_outline", lambda *a, **kw: {
        "algorithm_version": "llm-outline-v1.0",
        "char_count": len(SCRIPT_TEXT),
        "dialogue_char_estimate": FAKE_OUTLINE_OUTPUT["dialogue_char_estimate"],
        "estimated_total_duration": FAKE_OUTLINE_OUTPUT["estimated_total_duration"],
        "estimated_episodes": FAKE_OUTLINE_OUTPUT["total_episodes"],
        "episode_breakdown": [
            {"episode": e["episode"], "title": e["title"],
             "estimated_duration": e["estimated_duration"],
             "estimated_shots": e["estimated_shots"]}
            for e in FAKE_OUTLINE_OUTPUT["episodes"]
        ],
        "exceeds_batch_limit": False,
        "suggestion": None,
    })


def _confirmed_estimate(client: TestClient, pid: str, sv: dict, monkeypatch) -> None:
    _mock_outline(monkeypatch)
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


def test_estimate_hash_validation(client, monkeypatch):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    # 文本与版本哈希不符 → 409 script_version_conflict
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": "完全不同的文本"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "script_version_conflict"
    # 正常评估（mock LLM 大纲分析）
    _mock_outline(monkeypatch)
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT})
    assert r.status_code == 201
    body = r.json()
    assert body["char_count"] == len(SCRIPT_TEXT)
    assert body["estimated_episodes"] >= 1
    assert body["user_confirmed"] is False
    assert len(body["episode_breakdown"]) == body["estimated_episodes"]


def test_parse_allows_without_confirmed_estimate(client, monkeypatch):
    """口径变更：解析不再强制前置确认评估（集数由解析器自主分析），未评估直接解析返回 202。"""
    pid = _make_project(client)
    sv = _save_script(client, pid)
    # 未评估直接解析 → 202 入队
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
    # 评估后解析同样返回 202，不受是否确认评估影响
    _mock_outline(monkeypatch)
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT})
    assert r.status_code == 201
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    assert r.status_code == 202


def test_parse_job_lifecycle(client, monkeypatch):
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv, monkeypatch)
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
    _confirmed_estimate(client, pid, sv, monkeypatch)
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
    _confirmed_estimate(client, pid, sv, monkeypatch)
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
    _confirmed_estimate(client, pid, sv, monkeypatch)
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


# ── 剧本解析接入项目风格配置：快照与差异落库 ──────────────────
FAKE_PARSE_OUTPUT_STYLE = {
    **FAKE_PARSE_OUTPUT,
    "project_analysis": {
        **FAKE_PARSE_OUTPUT["project_analysis"],
        "detected_genre_tags": ["古装权谋"],
        "detected_narrative_styles": ["悬念反转型"],
    },
}


def _run_parse_result(client, monkeypatch, pid, sv, fake_output) -> dict:
    """提交解析任务并用替身执行，返回 job.result 全量内容。"""
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
    return client.get(f"/api/jobs/{job_id}").json()["result"]


def test_parse_result_contains_style_snapshot_and_diffs(client, monkeypatch):
    """项目配置了风格时，解析结果须带风格快照，并报出与配置不符的识别项。"""
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv, monkeypatch)
    r = client.put(f"/api/projects/{pid}/style-config", json={
        "genre_tags": ["校园青春"],
        "narrative_styles": ["温情治愈型"],
        "visual_style": "anime_2d",
        "pacing": "tight",
        "emotional_tone": ["紧张", "热血"],
    })
    assert r.status_code == 200
    result = _run_parse_result(client, monkeypatch, pid, sv, FAKE_PARSE_OUTPUT_STYLE)
    snap = result["style_config_snapshot"]
    assert snap["project_id"] == pid
    assert snap["visual_style"] == "anime_2d"
    assert snap["visual_style_label"]
    diffs = {d["field"]: d for d in result["style_config_diffs"]}
    assert set(diffs) == {"genre_tags", "narrative_styles"}
    assert diffs["genre_tags"]["detected"] == ["古装权谋"]
    assert diffs["genre_tags"]["configured"] == ["校园青春"]
    assert diffs["narrative_styles"]["detected"] == ["悬念反转型"]


def test_parse_result_default_style_snapshot(client, monkeypatch):
    """未配置风格的新项目：快照回退默认视觉风格，且无识别字段时不报差异。"""
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv, monkeypatch)
    result = _run_parse_result(client, monkeypatch, pid, sv, FAKE_PARSE_OUTPUT)
    snap = result["style_config_snapshot"]
    assert snap["visual_style"] == "storybook_realism"
    assert snap["version"] == "style-v1.0"
    assert result["style_config_diffs"] == []


def test_parse_result_no_detected_fields_no_diff(client, monkeypatch):
    """配置了风格但 LLM 未输出 detected_* 时，不凭猜测报差异。"""
    pid = _make_project(client)
    sv = _save_script(client, pid)
    _confirmed_estimate(client, pid, sv, monkeypatch)
    r = client.put(f"/api/projects/{pid}/style-config",
                   json={"genre_tags": ["校园青春"], "visual_style": "chinese_ink"})
    assert r.status_code == 200
    result = _run_parse_result(client, monkeypatch, pid, sv, FAKE_PARSE_OUTPUT)
    assert result["style_config_snapshot"]["visual_style"] == "chinese_ink"
    assert result["style_config_diffs"] == []
