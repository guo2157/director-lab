"""系列续写 mock 全流程端到端测试。

目的：在不依赖真实语言模型与后台线程的前提下，跑通完整链路：
创建系列 → 创建剧集 → 上下文预览 → 发起续写（202）→ 同步执行 series_agent Job
→ 四阶段全部成功 → 上下文清单与剧本版本落库 → 确认剧本（乐观锁 + 快照 + 事件入账）
→ 重复确认命中幂等 → 乐观锁拒绝过期版本 → 未完成视频不阻止下一集创作。

mock 纪律：只替换 LLMClient，不跳过 Context Builder、连续性检查、StateCommitService
等任何真实逻辑，确保测试验证的是真实链路而非桩。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Job
from app.worker.series_executor import execute_series_agent_job

MOCK_LLM_RESPONSE = {
    "outline": "小熊在森林里找到遗失的铃铛，并交还给小兔。",
    "synopsis": "小熊找回铃铛并交还小兔。",
    "script": "场景一：森林入口。小熊低头看见草丛里有东西在反光。\n场景二：老树下。小熊把铃铛递给小兔。",
    "scenes": ["森林入口", "老树下"],
    "characters": ["小熊", "小兔"],
    "props": ["铃铛"],
    "events": [
        {
            "event_key": "ev_bell_found",
            "event_type": "prop_acquired",
            "title": "小熊找到铃铛",
            "description": "小熊在老树下发现铃铛",
            "importance": 4,
            "involved_asset_names": ["铃铛"],
        },
        {
            "event_key": "ev_bell_returned",
            "event_type": "prop_transferred",
            "title": "铃铛交还小兔",
            "description": "小熊把铃铛交给小兔",
            "importance": 5,
            "involved_asset_names": ["铃铛", "小兔"],
        },
    ],
    "plot_thread_updates": [],
    "self_check": {"identity_kept": True, "prop_state_kept": True, "notes": "mock"},
}


class FakeLLMClient:
    """确定性 LLM 替身：只实现 chat_json，返回固定 JSON 结构。"""

    def __init__(self, *args, **kwargs):
        self.model = "mock-llm"

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        return dict(MOCK_LLM_RESPONSE)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立 SQLite 库 + 替换 SessionLocal + 替换 LLMClient。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'series_e2e.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)

    # 执行器在模块导入时就绑定了 SessionLocal，必须单独替换
    import app.worker.series_executor as exec_mod
    monkeypatch.setattr(exec_mod, "SessionLocal", factory)

    # 写作服务在模块导入时就绑定了 LLMClient，必须单独替换
    import app.services.series.episode_writing_service as writing_mod
    monkeypatch.setattr(writing_mod, "LLMClient", FakeLLMClient)

    client = TestClient(app)
    yield client, factory
    engine.dispose()


def _create_series(client: TestClient) -> dict:
    r = client.post("/api/series", json={"name": "小熊成长记", "genre": "儿童", "style": "水彩"})
    assert r.status_code == 201, r.text
    return r.json()


def test_mock_full_continue_to_confirm_flow(env):
    client, factory = env

    # ---------- 1. 创建系列（自动建立 Bible V1） ----------
    series = _create_series(client)
    series_id = series["id"]
    assert series_id.startswith("srs_")
    assert series["state_version"] == 1
    assert series["status"] == "active"

    # ---------- 2. 创建剧集 ----------
    r = client.post(f"/api/series/{series_id}/episodes", json={"title": "铃铛"})
    assert r.status_code == 201, r.text
    episode = r.json()
    episode_id = episode["id"]
    assert episode_id.startswith("eps_")
    assert episode["episode_number"] == 1
    assert episode["script_status"] == "draft"
    assert episode["production_status"] == "not_started"

    # ---------- 3. 上下文预览（与正式生成同规则，不落库） ----------
    r = client.get(
        f"/api/series/{series_id}/context-preview",
        params={"target_episode_number": 1, "user_instruction": "小熊找到铃铛并交还小兔"},
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["context_hash"]
    assert "series_bible" in preview
    assert "continuity" in preview

    # ---------- 4. 发起续写（202，不同步等待） ----------
    r = client.post(
        f"/api/series/{series_id}/episodes/continue",
        json={
            "target_episode_number": 1,
            "title": "铃铛",
            "user_instruction": "小熊找到铃铛并交还小兔",
            "direction": "custom",
        },
    )
    assert r.status_code == 202, r.text
    started = r.json()
    task_id = started["task_id"]
    assert started["episode_id"] == episode_id
    assert started["status"] == "context_building"

    # 任务已创建但尚未执行
    r = client.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] in ("queued", "pending")

    # ---------- 5. 同步执行 series_agent Job（mock LLM） ----------
    db = factory()
    try:
        jobs = db.query(Job).filter(Job.job_type == "series_agent").all()
        assert len(jobs) == 1
        job = jobs[0]
        assert (job.params or {})["task_id"] == task_id
        assert job.status == "queued"
        job_id = job.id
    finally:
        db.close()

    result = execute_series_agent_job(job_id)
    assert result["ok"] is True, result

    # ---------- 6. 任务真实进度与四阶段状态 ----------
    r = client.get(f"/api/tasks/{task_id}")
    assert r.status_code == 200
    task = r.json()
    assert task["status"] == "completed"
    assert task["progress"] == 100
    stages = {s["stage_key"]: s["status"] for s in task["stages"]}
    assert stages == {
        "context_building": "succeeded",
        "writing": "succeeded",
        "validating": "succeeded",
        "awaiting_review": "succeeded",
    }
    assert task["result"]["result_type"] == "series_script_draft"
    assert task["result"]["episode_id"] == episode_id

    # ---------- 7. 上下文清单落库且可追溯 ----------
    r = client.get(f"/api/episodes/{episode_id}/context-manifest")
    assert r.status_code == 200, r.text
    manifest = r.json()
    assert manifest["id"].startswith("ecm_")
    assert manifest["context_hash"]
    assert manifest["context_version"] >= 1

    # ---------- 8. 剧集与剧本版本状态 ----------
    r = client.get(f"/api/episodes/{episode_id}")
    assert r.status_code == 200
    ep = r.json()
    assert ep["script_status"] == "awaiting_review"
    assert ep["script_current_version_id"]
    assert ep["context_manifest_id"] == manifest["id"]
    # 续写会自动建立生产工作空间，但生产状态保持独立
    assert ep["project_id"]
    assert ep["production_status"] == "not_started"

    r = client.get(f"/api/episodes/{episode_id}/scripts")
    assert r.status_code == 200
    scripts = r.json()
    assert len(scripts) == 1
    script = scripts[0]
    assert script["id"].startswith("ssv_")
    assert script["status"] == "awaiting_review"
    assert script["source_context_manifest_id"] == manifest["id"]
    assert script["prompt_template_version"] == "episode_continue_v1"
    assert script["model"] == "mock-llm"
    assert script["content"].startswith("场景一")
    events = script["validation_result"]["events"]
    assert len(events) == 2
    # Agent 只能产出候选，事件事实状态必须仍是 proposed
    assert {e["fact_status"] for e in events} == {"proposed"}
    assert {e["event_key"] for e in events} == {"ev_bell_found", "ev_bell_returned"}

    # ---------- 9. 连续性检查结果已回写 ----------
    r = client.get(f"/api/episodes/{episode_id}/continuity/issues")
    assert r.status_code == 200
    issues_payload = r.json()
    # 首集无快照：预期为 warning（非 error），因此不阻断确认
    assert issues_payload["status"] == "warning"
    assert all(i["severity"] != "error" for i in issues_payload["issues"])

    # ---------- 10. 乐观锁：过期状态版本被拒绝 ----------
    r = client.post(
        f"/api/episodes/{episode_id}/scripts/{script['id']}/confirm",
        json={"expected_series_state_version": 99},
    )
    assert r.status_code >= 400, r.text

    # ---------- 11. 正常确认：快照 + 事件入账 + 状态推进 ----------
    r = client.post(
        f"/api/episodes/{episode_id}/scripts/{script['id']}/confirm",
        json={"expected_series_state_version": 1, "review_note": "mock 全流程确认"},
    )
    assert r.status_code == 200, r.text
    confirmed = r.json()
    assert confirmed["script_status"] == "confirmed"
    assert confirmed["series_state_version"] == 2
    assert confirmed["snapshot_version"] == 1
    assert confirmed["committed_event_count"] == 2

    # 候选事实在用户确认后成为正式事实
    r = client.get(f"/api/series/{series_id}/story-events")
    assert r.status_code == 200
    story_events = r.json()
    assert len(story_events) == 2
    assert {e["fact_status"] for e in story_events} == {"confirmed"}
    assert [e["occurred_order"] for e in story_events] == [1, 2]
    assert {e["episode_id"] for e in story_events} == {episode_id}

    # 剧集指针与连续性状态
    r = client.get(f"/api/episodes/{episode_id}")
    ep = r.json()
    assert ep["script_status"] == "confirmed"
    assert ep["confirmed_script_version_id"] == script["id"]
    assert ep["continuity_status"] == "warning"

    # 系列指针与概览
    r = client.get(f"/api/series/{series_id}/continuity")
    assert r.status_code == 200
    overview = r.json()
    assert overview["state_version"] == 2
    assert overview["confirmed_event_count"] == 2
    assert overview["latest_snapshot"]["version"] == 1
    assert overview["episodes_with_conflict"] == 0

    r = client.get(f"/api/series/{series_id}/continuity/snapshots")
    assert r.status_code == 200
    snapshots = r.json()
    assert len(snapshots) == 1
    assert snapshots[0]["version"] == 1
    assert snapshots[0]["based_on_episode_id"] == episode_id
    assert snapshots[0]["state_hash"]

    # ---------- 12. 幂等重放：重复确认不重复入账 ----------
    r = client.post(
        f"/api/episodes/{episode_id}/scripts/{script['id']}/confirm",
        json={"expected_series_state_version": 2},
    )
    assert r.status_code == 200, r.text
    replay = r.json()
    assert replay["committed_event_count"] == 0
    assert replay["series_state_version"] == 2

    r = client.get(f"/api/series/{series_id}/story-events")
    assert len(r.json()) == 2  # 未新增事件

    # ---------- 13. 视频未完成不阻止下一集创作 ----------
    r = client.post(
        f"/api/series/{series_id}/episodes/continue",
        json={"user_instruction": "第二集：小熊把铃铛送去修理", "title": "修铃铛"},
    )
    assert r.status_code == 202, r.text
    second = r.json()
    assert second["episode_number"] == 2

    # ---------- 14. 系列派生统计 ----------
    r = client.get(f"/api/series/{series_id}")
    assert r.status_code == 200
    stats = r.json()
    assert stats["episode_count"] == 2
    assert stats["confirmed_script_count"] == 1
    assert stats["completed_video_count"] == 0
    assert stats["bible_version"] == 1
    assert stats["state_version"] == 2


def test_mock_regenerate_rejected_on_confirmed_script(env):
    """已确认剧本不允许重新生成覆盖。"""
    client, factory = env
    series = _create_series(client)
    series_id = series["id"]

    r = client.post(f"/api/series/{series_id}/episodes", json={"title": "第一集"})
    episode_id = r.json()["id"]

    r = client.post(
        f"/api/series/{series_id}/episodes/continue",
        json={"target_episode_number": 1, "user_instruction": "小熊出门"},
    )
    assert r.status_code == 202

    db = factory()
    try:
        job_id = db.query(Job).filter(Job.job_type == "series_agent").one().id
    finally:
        db.close()
    assert execute_series_agent_job(job_id)["ok"] is True

    script = client.get(f"/api/episodes/{episode_id}/scripts").json()[0]
    r = client.post(
        f"/api/episodes/{episode_id}/scripts/{script['id']}/confirm",
        json={"expected_series_state_version": 1},
    )
    assert r.status_code == 200, r.text

    r = client.post(
        f"/api/episodes/{episode_id}/scripts/{script['id']}/regenerate",
        json={"user_instruction": "重写一版"},
    )
    assert r.status_code >= 400, r.text


def test_mock_context_preview_does_not_persist_manifest(env):
    """上下文预览不得写库（与正式生成同一规则但不落库）。"""
    client, factory = env
    series = _create_series(client)
    series_id = series["id"]
    r = client.post(f"/api/series/{series_id}/episodes", json={"title": "第一集"})
    episode_id = r.json()["id"]

    r = client.get(
        f"/api/series/{series_id}/context-preview",
        params={"target_episode_number": 1, "user_instruction": "预览"},
    )
    assert r.status_code == 200
    assert r.json()["context_hash"]

    # 预览后仍无清单记录
    assert client.get(f"/api/episodes/{episode_id}/context-manifest").status_code >= 400
