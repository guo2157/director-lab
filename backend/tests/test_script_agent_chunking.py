"""阶段 1 验收测试：_parse_batch 只发送本批集正文（按集切片）。

对照旧行为：旧实现 `hasattr(sv, "get_episode_chunk")` 恒为 False，每一批都
发送整本剧本。这里用替身 LLMClient 跑一个 5 集剧本，断言：
- 每次解析请求的 user_prompt 只含对应集正文，不含其他集独有字符串
- 解析请求长度明显小于全文
- prompt 不再出现「剧本全文」字样
- 写入了 agent.chunking 事件（mode=markers）
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, ScriptVersion, ids

# 每集带一个独有字符串，用于断言「没有串集」
SCRIPT_5 = """人物表：
林昭——女主
陈默——男主

第1集 归途
场次1 黄昏 车站
林昭：EZ1_我回来了。
陈默：EZ1_三年了。

第2集 旧信
场次1 夜 书房
林昭：EZ2_这封信是谁写的？

第3集 对峙
场次1 日 天台
陈默：EZ3_别再查了。

第4集 真相
场次1 夜 仓库
林昭：EZ4_原来是你。

第5集 落幕
场次1 黎明 海边
陈默：EZ5_结束了。
"""


class _Meta:
    """模拟 LLMClient.last_metadata（call_with_retry 需要读用量字段）。"""

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
        "total_episodes": 5,
        "episodes": [
            {
                "episode": i,
                "title": f"标题{i}",
                "estimated_duration": 90,
                "estimated_shots": 2,
            }
            for i in range(1, 6)
        ],
        "dialogue_char_estimate": 1000,
    }


def _parse_output(b_start: int, b_end: int) -> dict:
    """构造通过 validate_parse_payload 的解析结果（每集 1 场景 2 镜头）。"""
    scenes = []
    eps = []
    for ep in range(b_start, b_end + 1):
        scenes.append({
            "episode": ep,
            "title": f"第{ep}集场景",
            "description": f"第{ep}集场景描述",
            "time_note": "夜",
            "shots": [
                {"episode": ep, "seq": 1, "scene_id": f"第{ep}集场景", "duration": 5,
                 "image_prompt": f"ep{ep} 镜1", "shot_kind": "visual"},
                {"episode": ep, "seq": 2, "scene_id": f"第{ep}集场景", "duration": 5,
                 "image_prompt": f"ep{ep} 镜2", "shot_kind": "visual"},
            ],
        })
        eps.append({"episode": ep, "goal": f"目标{ep}", "rhythm_curve": "平稳",
                    "cliffhanger_type": "信息缺失", "hook_to_next": "", "locked": False})
    return {
        "project_analysis": {"summary": "测试", "theme": "测试",
                             "highlights": [], "foreshadowing": []},
        "characters": [{"name": "林昭", "description": "女主",
                        "traits": "", "appearance": ""}],
        "props": [],
        "scenes": scenes,
        "episodes": eps,
    }


class FakeLLMClient:
    """替身客户端：按 user_prompt 特征分流大纲 / 解析 / 评估，并记录收到的提示词。"""

    instances: list["FakeLLMClient"] = []
    # 大纲分析 / 评估 / 解析各自 new 一个 LLMClient，解析请求分散在不同实例上，
    # 因此用类级累加器收集，避免误取到「大纲那个实例」的空列表。
    all_parse_prompts: list[str] = []

    def __init__(self, *args, **kwargs):
        self.max_tokens = 32768
        self.last_metadata = None
        self.parse_prompts: list[str] = []
        self.outline_prompts: list[str] = []
        FakeLLMClient.instances.append(self)

    def _meta(self, chars: int) -> None:
        self.last_metadata = _Meta(chars)

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        self._meta(len(user_prompt))
        if "分集结构" in user_prompt:
            self.outline_prompts.append(user_prompt)
            return _outline()
        self.outline_prompts.append(user_prompt)
        return _outline()

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None, should_cancel=None, cancel_token=None):
        self._meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            self.parse_prompts.append(user_prompt)
            FakeLLMClient.all_parse_prompts.append(user_prompt)
            m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
            assert m, "解析 prompt 缺少集范围"
            b_start, b_end = int(m.group(1)), int(m.group(2))
            payload = json.dumps(_parse_output(b_start, b_end), ensure_ascii=False)
            if on_delta is not None:
                on_delta(payload)
            return payload
        # 评估等其他调用：返回结构合法的 JSON 文本
        body = json.dumps({
            "strengths": [], "issues": [], "suggestions": [],
            "production_risks": [], "plot_holes": [],
            "overall_score": 8, "summary": "测试评估",
        }, ensure_ascii=False)
        if on_delta is not None:
            on_delta(body)
        return body


class _Settings:
    """最小配置桩：单集一批 + 解析全部批次（旧行为），关闭重试以加速测试。"""

    max_episodes_per_batch = 1
    agent_first_batch_auto = False
    llm_parse_concurrency = 1
    llm_parse_retry_limit = 0
    llm_max_tokens = 32768
    llm_parse_max_tokens = 65536
    llm_parse_hard_max_tokens = 131072
    llm_timeout_seconds = 30
    llm_stream_idle_timeout_seconds = 30


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'chunking.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod
    import app.services.script_agent as sa
    import app.integrations.llm as llm_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(sa, "SessionLocal", maker)
    # 大纲分析在函数内 from app.integrations.llm import LLMClient，需同时替换两处
    monkeypatch.setattr(llm_mod, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(sa, "LLMClient", FakeLLMClient)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())

    session = maker()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="切片项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot=SCRIPT_5, content_hash="hash_chunk_5", created_at=now,
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


@pytest.fixture()
def run_job(env):
    session, project, sv, job, maker = env
    FakeLLMClient.instances.clear()
    FakeLLMClient.all_parse_prompts.clear()
    import app.services.script_agent as sa

    result = sa.execute_script_agent_job(job.id)
    return maker, job.id, result


def test_parse_prompt_contains_only_own_episode(run_job):
    maker, job_id, result = run_job
    prompts = FakeLLMClient.all_parse_prompts
    # 5 集 5 批（max_episodes_per_batch=1 且 agent_first_batch_auto=False）
    assert len(prompts) == 5
    for prompt in prompts:
        m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", prompt)
        assert m
        b_start, b_end = int(m.group(1)), int(m.group(2))
        assert b_start == b_end
        ep = b_start
        # 含本集独有字符串
        assert f"EZ{ep}_" in prompt
        # 不含任何其他集独有字符串
        for other in range(1, 6):
            if other != ep:
                assert f"EZ{other}_" not in prompt


def test_parse_prompt_body_is_much_shorter_than_full_text(run_job):
    """验收口径：prompt ≈ 1/5 全文 + 固定头。

    小样例里固定头（风格块 + 全局上下文）本身就比单集正文长，因此只比较
    「正文部分」与全文长度——这才是切片真正省下来的 token。
    """
    maker, job_id, result = run_job
    prompts = FakeLLMClient.all_parse_prompts
    assert len(prompts) == 5
    full_len = len(SCRIPT_5)
    for prompt in prompts:
        body = prompt.split("集剧本：", 1)[1]
        # 单集正文约为全文的 1/5；给到 0.45 的余量仍能证明「没有带整本」
        assert len(body) < full_len * 0.45


def test_prompt_no_longer_says_full_script(run_job):
    maker, job_id, result = run_job
    prompts = FakeLLMClient.all_parse_prompts
    assert prompts
    for prompt in prompts:
        assert "剧本全文" not in prompt
        assert "集剧本：" in prompt


def test_prompt_total_length_bounded_by_body_plus_fixed_header(run_job):
    """完整 prompt = 固定头 + 本集正文；不随集数增长（旧实现是 5 倍全文）。"""
    maker, job_id, result = run_job
    prompts = FakeLLMClient.all_parse_prompts
    assert len(prompts) == 5
    total = len(SCRIPT_5)
    for prompt in prompts:
        body = prompt.split("集剧本：", 1)[1]
        header = len(prompt) - len(body)
        assert header > 0
        # 头部固定不随集数膨胀，整体也不应接近「整本 × 集数」的量级
        assert len(prompt) < header + total * 0.45


def test_global_context_includes_preamble_and_titles(run_job):
    maker, job_id, result = run_job
    prompts = FakeLLMClient.all_parse_prompts
    assert len(prompts) == 5
    first = prompts[0]
    # 人物表（preamble）作为全局上下文传入，不丢弃
    assert "人物表" in first
    assert "林昭" in first
    # 本批集在大纲中的标题
    assert "标题1" in first
    # 不应带其他集的大纲标题
    assert "标题3" not in first


def test_chunking_event_written(run_job):
    maker, job_id, result = run_job
    session = maker()
    try:
        events = (
            session.query(Job)
            .filter(Job.id == job_id).one()
        )
        assert events.status == "succeeded"
        from app.models import JobEvent
        chunk_events = (
            session.query(JobEvent)
            .filter(JobEvent.job_id == job_id, JobEvent.event_type == "agent.chunking")
            .all()
        )
        assert len(chunk_events) == 1
        payload = chunk_events[0].payload
        assert payload["mode"] == "markers"
        assert payload["chunk_count"] == 5
        assert payload["preamble_chars"] > 0
        assert len(payload["batch_chars"]) == 5
        for item in payload["batch_chars"]:
            assert item["chars"] > 0
    finally:
        session.close()


def test_job_succeeded_with_all_episodes(run_job):
    maker, job_id, result = run_job
    assert result.get("ok") is True
    session = maker()
    try:
        job = session.query(Job).filter(Job.id == job_id).one()
        assert job.status == "succeeded"
        assert len(job.result.get("episodes") or []) == 5
    finally:
        session.close()
