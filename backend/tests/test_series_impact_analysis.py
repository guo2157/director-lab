"""历史剧本修改影响分析测试（只读服务）。

覆盖：
- 与已确认版本对比时返回事件键差异（diff）
- 分析过程不写任何正式剧情状态（state_version、事件数、快照数均不变）
- 新版本移除事件键且后续剧集已确认时给出 warning
- 无已确认基线时返回 info 且不影响后续判断
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Job
from app.worker.series_executor import execute_series_agent_job


MOCK_LLM_RESPONSE = {
    "outline": "mock 大纲",
    "synopsis": "mock 梗概",
    "script": "场景一：mock 剧本正文。",
    "scenes": ["森林"],
    "characters": ["小熊"],
    "props": ["铃铛"],
    "events": [
        {
            "event_key": "ev_a",
            "event_type": "prop_acquired",
            "title": "获得铃铛",
            "description": "小熊获得铃铛",
            "importance": 4,
            "involved_asset_names": ["铃铛"],
        },
        {
            "event_key": "ev_b",
            "event_type": "plot_development",
            "title": "线索推进",
            "description": "剧情推进",
            "importance": 3,
            "involved_asset_names": [],
        },
    ],
    "plot_thread_updates": [],
    "self_check": {"identity_kept": True, "prop_state_kept": True, "notes": "mock"},
}


class FakeLLMClient:
    def __init__(self, *args, **kwargs):
        self.model = "mock-llm"

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        return dict(MOCK_LLM_RESPONSE)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'impact.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)

    import app.worker.series_executor as exec_mod

    monkeypatch.setattr(exec_mod, "SessionLocal", factory)

    import app.services.series.episode_writing_service as writing_mod

    monkeypatch.setattr(writing_mod, "LLMClient", FakeLLMClient)

    yield TestClient(app), factory
    engine.dispose()


def _run_episode(client: TestClient, factory, series_id: str, number: int) -> tuple[str, str]:
    """创建并完整执行一集续写，返回 (episode_id, script_id)，已完成确认。"""
    r = client.post(
        f"/api/series/{series_id}/episodes", json={"episode_number": number, "title": f"第{number}集"}
    )
    assert r.status_code == 201, r.text
    episode_id = r.json()["id"]

    r = client.post(
        f"/api/series/{series_id}/episodes/continue",
        json={"target_episode_number": number, "user_instruction": "继续创作"},
    )
    assert r.status_code == 202, r.text

    db = factory()
    try:
        job_id = (
            db.query(Job)
            .filter(Job.job_type == "series_agent")
            .order_by(Job.created_at.desc())
            .first()
            .id
        )
    finally:
        db.close()

    assert execute_series_agent_job(job_id)["ok"] is True

    script = client.get(f"/api/episodes/{episode_id}/scripts").json()[0]
    series = client.get(f"/api/series/{series_id}").json()
    r = client.post(
        f"/api/episodes/{episode_id}/scripts/{script['id']}/confirm",
        json={"expected_series_state_version": series["state_version"]},
    )
    assert r.status_code == 200, r.text
    return episode_id, script["id"]


def _snapshot_of(client: TestClient, series_id: str) -> tuple[int, int, int]:
    overview = client.get(f"/api/series/{series_id}/continuity").json()
    events = client.get(f"/api/series/{series_id}/story-events").json()
    snapshots = client.get(f"/api/series/{series_id}/continuity/snapshots").json()
    return overview["state_version"], len(events), len(snapshots)


def test_impact_analysis_is_read_only_and_diffs_versions(env):
    client, factory = env
    series_id = client.post("/api/series", json={"name": "影响分析系列"}).json()["id"]
    episode_id, confirmed_script_id = _run_episode(client, factory, series_id, 1)

    before = _snapshot_of(client, series_id)

    r = client.post(
        f"/api/episodes/{episode_id}/scripts",
        json={"content": "改写版本正文", "synopsis": "改写"},
    )
    assert r.status_code == 201, r.text
    draft_id = r.json()["id"]

    r = client.get(f"/api/episodes/{episode_id}/scripts/{draft_id}/impact")
    assert r.status_code == 200, r.text
    payload = r.json()

    assert payload["diff"]["base_script_version_id"] == confirmed_script_id
    assert payload["diff"]["base_version"] == 1
    assert payload["diff"]["compare_script_version_id"] == draft_id
    assert set(payload["diff"]["removed_event_keys"]) == {"ev_a", "ev_b"}
    assert payload["diff"]["added_event_keys"] == []
    assert payload["status"] in ("consistent", "warning")

    after = _snapshot_of(client, series_id)
    assert before == after


def test_impact_analysis_warns_removed_events_with_later_episodes(env):
    client, factory = env
    series_id = client.post("/api/series", json={"name": "多集系列"}).json()["id"]
    ep1_id, _ = _run_episode(client, factory, series_id, 1)
    _run_episode(client, factory, series_id, 2)

    before = _snapshot_of(client, series_id)

    r = client.post(
        f"/api/episodes/{ep1_id}/scripts", json={"content": "改写第一集"}
    )
    draft_id = r.json()["id"]

    r = client.get(f"/api/episodes/{ep1_id}/scripts/{draft_id}/impact")
    assert r.status_code == 200, r.text
    payload = r.json()

    types = {i["type"] for i in payload["issues"]}
    assert "removed_event_key_with_later_events" in types
    assert payload["status"] == "warning"

    affected = {e["episode_number"] for e in payload["affected_episodes"]}
    assert affected == {2}

    assert before == _snapshot_of(client, series_id)


def test_impact_analysis_without_confirmed_baseline(env):
    client, factory = env
    series_id = client.post("/api/series", json={"name": "无基线系列"}).json()["id"]
    episode_id = client.post(
        f"/api/series/{series_id}/episodes", json={"episode_number": 1}
    ).json()["id"]

    r = client.post(
        f"/api/episodes/{episode_id}/scripts", json={"content": "第一版草稿"}
    )
    script_id = r.json()["id"]

    r = client.get(f"/api/episodes/{episode_id}/scripts/{script_id}/impact")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["diff"]["base_script_version_id"] is None
    assert payload["status"] == "consistent"
    assert {i["type"] for i in payload["issues"]} == {"no_confirmed_baseline"}
    assert payload["affected_episodes"] == []
