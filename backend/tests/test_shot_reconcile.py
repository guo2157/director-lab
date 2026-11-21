"""阶段 4 测试：镜头数对账（实际镜头数 vs 大纲预估镜头数）。

覆盖：
- 实际镜头数显著低于大纲预估 → parse_warnings 记 shot_count_low、
  job.result.low_shot_episodes 列出明细、写 agent.shot_reconcile 事件
- 实际镜头数达到阈值 → 不告警、low_shot_episodes 为空
- parse_shot_ratio_warn = 0 → 关闭对账，不写事件也不告警
- 只对本批范围对账：解析第 2 批时不会把未解析的第 1 批误报为 0 镜
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, ScriptVersion, ids

SCRIPT_2 = """人物表：
林昭——女主

第1集 归途
场次1 黄昏 车站
林昭：EZ1_我回来了。

第2集 旧信
场次1 夜 书房
林昭：EZ2_这封信是谁写的？
"""


class _Meta:
    def __init__(self, prompt_chars: int = 500):
        self.model = "fake-model"
        self.max_tokens = 0
        self.finish_reason = "stop"
        self.completion_tokens = 200
        self.reasoning_tokens = 0
        self.prompt_tokens = max(1, prompt_chars // 2)
        self.content_length = 0
        self.reasoning_length = 0


def _outline(estimated_shots: int) -> dict:
    return {
        "total_episodes": 2,
        "estimated_total_duration": 180,
        "dialogue_char_estimate": 400,
        "episodes": [
            {"episode": i, "title": f"标题{i}",
             "estimated_duration": 90, "estimated_shots": estimated_shots}
            for i in (1, 2)
        ],
    }


def _parse_output(b_start: int, b_end: int, shots_per_episode: int) -> dict:
    scenes = []
    episodes = []
    for ep in range(b_start, b_end + 1):
        scenes.append({
            "episode": ep,
            "title": f"第{ep}集场景",
            "description": f"第{ep}集场景描述",
            "time_note": "夜",
            "shots": [
                {"episode": ep, "seq": n + 1, "scene_id": f"第{ep}集场景",
                 "duration": 5, "image_prompt": f"ep{ep} 镜{n + 1}",
                 "shot_kind": "visual"}
                for n in range(shots_per_episode)
            ],
        })
        episodes.append({
            "episode": ep, "goal": f"目标{ep}", "rhythm_curve": "平稳",
            "cliffhanger_type": "信息缺失", "hook_to_next": "", "locked": False,
        })
    return {
        "project_analysis": {"summary": "测试", "theme": "测试",
                             "highlights": [], "foreshadowing": []},
        "characters": [{"name": "林昭", "description": "女主",
                        "traits": "", "appearance": ""}],
        "props": [],
        "scenes": scenes,
        "episodes": episodes,
    }


class FakeLLMClient:
    """替身：大纲返回固定预估镜头数，解析返回固定实际镜头数。"""

    estimated_shots = 10
    actual_shots = 2

    def __init__(self, *args, **kwargs):
        self.max_tokens = 32768
        self.last_metadata = None

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        self.last_metadata = _Meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
            assert m, "解析 prompt 缺少集范围"
            return _parse_output(int(m.group(1)), int(m.group(2)),
                                 FakeLLMClient.actual_shots)
        return _outline(FakeLLMClient.estimated_shots)

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None, should_cancel=None, cancel_token=None):
        self.last_metadata = _Meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
            assert m
            payload = json.dumps(
                _parse_output(int(m.group(1)), int(m.group(2)),
                              FakeLLMClient.actual_shots), ensure_ascii=False)
        else:
            payload = json.dumps(_outline(FakeLLMClient.estimated_shots),
                                 ensure_ascii=False)
        if on_delta is not None:
            on_delta(payload)
        return payload


class _Settings:
    max_episodes_per_batch = 1
    agent_first_batch_auto = False
    llm_parse_concurrency = 1
    llm_parse_retry_limit = 0
    llm_max_tokens = 32768
    llm_parse_max_tokens = 65536
    llm_parse_hard_max_tokens = 131072
    llm_timeout_seconds = 30
    llm_stream_idle_timeout_seconds = 30
    llm_context_window_tokens = 0  # 关闭输入预算预检，聚焦对账逻辑
    llm_input_budget_ratio = 0.6
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3
    parse_shot_ratio_warn = 0.5


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'shot_reconcile.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod
    import app.services.script_agent as sa
    import app.integrations.llm as llm_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(sa, "SessionLocal", maker)
    monkeypatch.setattr(llm_mod, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(sa, "LLMClient", FakeLLMClient)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())

    session = maker()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="对账项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot=SCRIPT_2, content_hash="hash_reconcile_2", created_at=now,
    )
    session.add(sv)
    job = Job(
        id=ids.new_id("job"), project_id=project.id, job_type="script_agent",
        params={"script_version_id": sv.id, "episode_start": 1},
        status="queued", created_at=now,
    )
    session.add(job)
    session.commit()
    yield session, project, sv, job, maker
    session.close()


def _run(env):
    session, project, sv, job, maker = env
    import app.services.script_agent as sa

    result = sa.execute_script_agent_job(job.id)
    return maker, job.id, result


def _events(session, job_id: str, event_type: str) -> list:
    from app.models import JobEvent

    return (
        session.query(JobEvent)
        .filter(JobEvent.job_id == job_id, JobEvent.event_type == event_type)
        .all()
    )


def test_low_shot_count_records_warning(env):
    """预估 10 镜、实际 2 镜（20% < 50%）→ 告警且不阻断任务。"""
    FakeLLMClient.estimated_shots = 10
    FakeLLMClient.actual_shots = 2
    maker, job_id, result = _run(env)

    assert result.get("ok") is True
    session = maker()
    try:
        job = session.query(Job).filter(Job.id == job_id).one()
        assert job.status == "succeeded"
        low = job.result.get("low_shot_episodes") or []
        assert [x["episode"] for x in low] == [1, 2]
        for item in low:
            assert item["actual"] == 2
            assert item["estimated"] == 10
            assert item["ratio"] == 0.2
        warnings = job.result.get("parse_warnings") or []
        assert any(w.startswith("shot_count_low") for w in warnings)
        assert sum(1 for w in warnings if w.startswith("shot_count_low")) == 2

        # 对账按「本批范围」整段做一次：一次任务执行只写一条事件，
        # 事件内列出范围内所有低镜头集（不是每个解析批次各写一条）。
        events = _events(session, job_id, "agent.shot_reconcile")
        assert len(events) == 1
        payload = events[0].payload or {}
        assert payload.get("threshold") == 0.5
        assert payload.get("range") == "1-2"
        assert [x["episode"] for x in payload.get("low_shot_episodes") or []] == [1, 2]
    finally:
        session.close()


def test_enough_shots_records_no_warning(env):
    """预估 2 镜、实际 2 镜（100%）→ 不告警。"""
    FakeLLMClient.estimated_shots = 2
    FakeLLMClient.actual_shots = 2
    maker, job_id, result = _run(env)

    assert result.get("ok") is True
    session = maker()
    try:
        job = session.query(Job).filter(Job.id == job_id).one()
        assert job.result.get("low_shot_episodes") == []
        warnings = job.result.get("parse_warnings") or []
        assert not any(w.startswith("shot_count_low") for w in warnings)
    finally:
        session.close()


def test_threshold_zero_disables_reconcile(env, monkeypatch):
    """阈值为 0 → 完全关闭对账，不写事件也不告警。"""
    FakeLLMClient.estimated_shots = 10
    FakeLLMClient.actual_shots = 1

    class _Off(_Settings):
        parse_shot_ratio_warn = 0

    monkeypatch.setattr("app.core.config.get_settings", lambda: _Off())
    maker, job_id, result = _run(env)

    assert result.get("ok") is True
    session = maker()
    try:
        job = session.query(Job).filter(Job.id == job_id).one()
        assert job.result.get("low_shot_episodes") == []
        warnings = job.result.get("parse_warnings") or []
        assert not any(w.startswith("shot_count_low") for w in warnings)
        assert _events(session, job_id, "agent.shot_reconcile") == []
    finally:
        session.close()


def test_reconcile_only_covers_current_batch(env):
    """只解析第 2 批时，不把未解析的第 1 集误报为 0 镜。"""
    FakeLLMClient.estimated_shots = 10
    FakeLLMClient.actual_shots = 2
    session, project, sv, job, maker = env
    job.params = {"script_version_id": sv.id, "episode_start": 2}
    session.commit()

    import app.services.script_agent as sa

    result = sa.execute_script_agent_job(job.id)
    assert result.get("ok") is True

    session2 = maker()
    try:
        row = session2.query(Job).filter(Job.id == job.id).one()
        low = row.result.get("low_shot_episodes") or []
        # 只有第 2 集在范围内；第 1 集绝不能被报成 0 镜
        assert [x["episode"] for x in low] == [2]
    finally:
        session2.close()
