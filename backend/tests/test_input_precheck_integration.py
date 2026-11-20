"""阶段 2 验收测试：输入预算预检在 script-agent 入口生效。

关键断言：
- 预检失败时**一次 LLM 都不调用**（超长输入不能先花掉一次请求再失败）
- 预检结果写入 agent.input_precheck 事件（含估算值、预算、字符数）
- 任务落到 failed + llm_input_too_long，而不是被当成可重试的供应商错误
- 预检通过时正常解析，且预检事件仍留痕
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, JobEvent, Project, ScriptVersion, ids

# 两集剧本，每集正文可调长度
def _script(per_ep_chars: int) -> str:
    return "\n".join(
        f"第{i}集 标题{i}\n场次1 夜 室内\n林昭：{'戏' * per_ep_chars}"
        for i in range(1, 3)
    )


class _Meta:
    def __init__(self, prompt_chars: int):
        self.model = "fake-model"
        self.max_tokens = 0
        self.finish_reason = "stop"
        self.completion_tokens = 200
        self.reasoning_tokens = 0
        self.prompt_tokens = max(1, prompt_chars // 2)
        self.content_length = 0
        self.reasoning_length = 0


def _outline() -> dict:
    return {
        "total_episodes": 2,
        "episodes": [
            {"episode": i, "title": f"标题{i}", "estimated_duration": 90,
             "estimated_shots": 1}
            for i in (1, 2)
        ],
        "dialogue_char_estimate": 100,
    }


def _parse_output(b_start: int, b_end: int) -> dict:
    scenes, eps = [], []
    for ep in range(b_start, b_end + 1):
        scenes.append({
            "episode": ep, "title": f"第{ep}集场景", "description": "描述",
            "time_note": "夜",
            "shots": [{"episode": ep, "seq": 1, "scene_id": f"第{ep}集场景",
                       "duration": 5, "image_prompt": "镜1", "shot_kind": "visual"}],
        })
        eps.append({"episode": ep, "goal": "目标", "rhythm_curve": "平稳",
                    "cliffhanger_type": "信息缺失", "hook_to_next": "", "locked": False})
    return {
        "project_analysis": {"summary": "测试", "theme": "测试",
                             "highlights": [], "foreshadowing": []},
        "characters": [{"name": "林昭", "description": "女主", "traits": "", "appearance": ""}],
        "props": [], "scenes": scenes, "episodes": eps,
    }


class CountingLLMClient:
    """记录所有调用的替身客户端——用于断言「一次都没调用」。"""

    calls: list[str] = []

    def __init__(self, *args, **kwargs):
        self.max_tokens = 32768
        self.last_metadata = None

    def _meta(self, chars: int) -> None:
        self.last_metadata = _Meta(chars)

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        CountingLLMClient.calls.append("chat_json")
        self._meta(len(user_prompt))
        return _outline()

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None):
        CountingLLMClient.calls.append("chat_stream")
        self._meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            import re
            m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
            payload = json.dumps(_parse_output(int(m.group(1)), int(m.group(2))),
                                 ensure_ascii=False)
        else:
            payload = json.dumps({
                "strengths": [], "issues": [], "suggestions": [],
                "production_risks": [], "plot_holes": [],
                "overall_score": 8, "summary": "评估",
            }, ensure_ascii=False)
        if on_delta is not None:
            on_delta(payload)
        return payload


class _Settings:
    max_episodes_per_batch = 2
    agent_first_batch_auto = False
    llm_parse_concurrency = 1
    llm_parse_retry_limit = 0
    llm_max_tokens = 32768
    llm_parse_max_tokens = 65536
    llm_parse_hard_max_tokens = 131072
    llm_timeout_seconds = 30
    llm_stream_idle_timeout_seconds = 30
    # 预检参数由各用例覆盖
    llm_context_window_tokens = 128000
    llm_input_budget_ratio = 0.6
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'precheck.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod
    import app.services.script_agent as sa
    import app.integrations.llm as llm_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(sa, "SessionLocal", maker)
    monkeypatch.setattr(llm_mod, "LLMClient", CountingLLMClient)
    monkeypatch.setattr(sa, "LLMClient", CountingLLMClient)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())
    CountingLLMClient.calls.clear()

    def _make(per_ep_chars: int):
        session = maker()
        now = datetime.now()
        project = Project(
            id=ids.project_id(), name="预检项目", aspect_ratio="9:16",
            creation_type="script", status="active", created_at=now, updated_at=now,
        )
        session.add(project)
        sv = ScriptVersion(
            id=ids.new_id("scv"), project_id=project.id, version=1,
            content_snapshot=_script(per_ep_chars), content_hash=f"h{per_ep_chars}",
            created_at=now,
        )
        session.add(sv)
        job = Job(
            id=ids.new_id("job"), project_id=project.id, job_type="script_agent",
            params={"script_version_id": sv.id, "episode_start": 1},
            status="queued", created_at=now,
        )
        session.add(job)
        session.commit()
        return session, job.id

    yield maker, _make


def _events(maker, job_id: str) -> dict[str, dict]:
    session = maker()
    try:
        rows = (
            session.query(JobEvent)
            .filter(JobEvent.job_id == job_id)
            .order_by(JobEvent.event_seq)
            .all()
        )
        return {r.event_type: (r.payload or {}) for r in rows}
    finally:
        session.close()


def test_precheck_blocks_before_any_llm_call(env, monkeypatch):
    """单集超预算：必须在发起任何 LLM 请求之前失败。"""
    maker, make = env
    # 预算压到 1000 token，单集正文远超
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: type("S", (_Settings,), {
            "llm_context_window_tokens": 1000, "llm_input_budget_ratio": 1.0,
        })(),
    )
    session, job_id = make(2000)
    session.close()

    import app.services.script_agent as sa
    result = sa.execute_script_agent_job(job_id)

    assert result["ok"] is False
    assert result["reason"] == "llm_input_too_long"
    # 核心断言：一次 LLM 都没调用
    assert CountingLLMClient.calls == []

    session = maker()
    job = session.get(Job, job_id)
    assert job.status == "failed"
    assert job.error_code == "llm_input_too_long"
    assert job.finished_at is not None
    session.close()

    events = _events(maker, job_id)
    assert "agent.input_precheck" in events
    payload = events["agent.input_precheck"]
    assert payload["ok"] is False
    assert payload["code"] == "llm_input_too_long"
    assert payload["budget"] == 1000
    assert payload["estimated_tokens"] > 1000
    assert payload["episode"] == 1


def test_precheck_event_written_on_success(env):
    """预检通过时也要留痕，便于事后核对估算与实际用量。"""
    maker, make = env
    session, job_id = make(50)
    session.close()

    import app.services.script_agent as sa
    result = sa.execute_script_agent_job(job_id)
    assert result["ok"] is True
    assert CountingLLMClient.calls  # 确实走了 LLM

    events = _events(maker, job_id)
    payload = events["agent.input_precheck"]
    assert payload["ok"] is True
    assert payload["code"] is None
    assert payload["budget"] == 76800  # 128000 × 0.6
    assert payload["estimated_tokens"] > 0
    assert payload["chars"] > 0
    assert payload["outline_mode"] == "full"


def test_precheck_disabled_when_window_zero(env, monkeypatch):
    """窗口置 0 关闭预检：极长正文也放行。"""
    maker, make = env
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: type("S", (_Settings,), {"llm_context_window_tokens": 0})(),
    )
    session, job_id = make(3000)
    session.close()

    import app.services.script_agent as sa
    result = sa.execute_script_agent_job(job_id)
    assert result["ok"] is True

    events = _events(maker, job_id)
    assert events["agent.input_precheck"]["budget"] == 0
    assert events["agent.input_precheck"]["ok"] is True
