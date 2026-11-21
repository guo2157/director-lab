"""锁定「切片为空不得回退全文」与「缺失集不得用近似段补齐」两条行为（2026-11-21 修复）。

## 背景一：切片为空回退全文

旧实现 `episode_text = slice_range(chunks, b_start, b_end) or text` 在切片为空时
把**整本剧本**发给单批。触发条件：集标记不连续（第1集 / 第3集 / 第5集）时，
大纲按 1..N 连续编号划批，落在缺失集号上的批次取不到内容。

长剧本下后果严重：本该 2 千字的批发出几万字，既可能超上下文预算，
又会让模型把无关剧情写进本集。

## 背景二：缺失集用 fallback_split 补齐会张冠李戴（比背景一更隐蔽）

旧实现在「标记数 < 大纲集数」时，把 `fallback_split(text, total)` 里集号未出现
的片段追加进来。但 fallback 是按全文均分出的**近似段**，与标记集号毫无对应关系。

实测（backend/_probe_truth.py）剧本只有第1/2集标记、大纲 4 集时：
    补齐进来的「第3集」实际装的是 **第2集的正文**
（fallback 把全文切成 4 份，第 3 份正好落在第2集上）

后果是第2集剧情被当成第3集重复解析、落库成重复镜头，比单纯漏集更难排查。

修复后：缺失集号如实记录进 `agent.chunking.missing_episodes`，
对应批次由 `_parse_batch` 明确失败，写入 `failed_ranges`。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, ScriptVersion, ids


def _outline_for(total: int) -> dict:
    """大纲结果。

    注意字段名：``script_agent`` 读的是 ``estimated_episodes``
    （见 script_agent.py「口径确定」段），``total_episodes`` 是 LLM 原始输出字段，
    两者不能混用——写错会让 total_episodes 回退成 1，批次划分失效。
    """
    return {
        "estimated_episodes": total,
        "total_episodes": total,
        "episodes": [
            {"episode": i, "title": f"标题{i}", "estimated_duration": 90,
             "estimated_shots": 2}
            for i in range(1, total + 1)
        ],
        "dialogue_char_estimate": 1000,
    }


class _FakeClient:
    """记录每次解析收到的 user_prompt，便于断言正文内容。"""

    all_parse_prompts: list[str] = []
    total_episodes = 1

    def __init__(self, *args, **kwargs):
        from tests.test_script_agent_chunking import _Meta
        self.max_tokens = 32768
        self.last_metadata = None

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        from tests.test_script_agent_chunking import _Meta
        self.last_metadata = _Meta(len(user_prompt))
        return _outline_for(self.total_episodes)

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None, should_cancel=None,
                    cancel_token=None):
        from tests.test_script_agent_chunking import _Meta, _parse_output
        self.last_metadata = _Meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            _FakeClient.all_parse_prompts.append(user_prompt)
            m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
            assert m, "解析 prompt 缺少集范围"
            b_start, b_end = int(m.group(1)), int(m.group(2))
            payload = json.dumps(_parse_output(b_start, b_end), ensure_ascii=False)
            if on_delta is not None:
                on_delta(payload)
            return payload
        body = json.dumps({
            "strengths": [], "issues": [], "suggestions": [],
            "production_risks": [], "plot_holes": [],
            "overall_score": 8, "summary": "测试评估",
        }, ensure_ascii=False)
        if on_delta is not None:
            on_delta(body)
        return body


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
    script_evaluation_enabled = False


def _make_env(tmp_path, monkeypatch, script: str, total_episodes: int):
    engine = make_engine(f"sqlite:///{tmp_path / 'nogofallback.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod
    import app.integrations.llm as llm_mod
    import app.services.script_agent as sa

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(sa, "SessionLocal", maker)
    monkeypatch.setattr(llm_mod, "LLMClient", _FakeClient)
    monkeypatch.setattr(sa, "LLMClient", _FakeClient)
    monkeypatch.setattr("app.core.config.get_settings", lambda: _Settings())

    _FakeClient.total_episodes = total_episodes
    _FakeClient.all_parse_prompts = []

    session = maker()
    now = datetime.now()
    project = Project(id=ids.project_id(), name="回退测试", aspect_ratio="9:16",
                      creation_type="script", status="active",
                      created_at=now, updated_at=now)
    session.add(project)
    sv = ScriptVersion(id=ids.new_id("scv"), project_id=project.id, version=1,
                       content_snapshot=script, content_hash=f"h{total_episodes}",
                       created_at=now)
    session.add(sv)
    job = Job(id=ids.new_id("job"), project_id=project.id, job_type="script_agent",
              params={"script_version_id": sv.id, "episode_start": 1},
              status="queued", created_at=now)
    session.add(job)
    session.commit()
    return session, project, sv, job, maker


def _body_of(prompt: str) -> str:
    """从解析 prompt 里取出剧本正文部分（「请解析以下剧本」之后）。"""
    idx = prompt.find("请解析以下剧本")
    return prompt[idx:] if idx >= 0 else prompt


def _chunking_event(session, job_id):
    from app.models import JobEvent
    rows = (session.query(JobEvent)
            .filter(JobEvent.job_id == job_id,
                    JobEvent.event_type == "agent.chunking")
            .all())
    assert rows, "未写入 agent.chunking 事件"
    payload = rows[-1].payload
    # payload 是 JSON 列：SQLAlchemy 已反序列化成 dict，无需再 json.loads
    return payload if isinstance(payload, dict) else json.loads(payload)


def _failed_ranges(session, job_id):
    """失败区间只写在 job.result 里，函数返回值不含该字段。

    注意：``execute_script_agent_job`` 的返回值是
    ``{ok, parse_version_id, shots, has_more}``，``failed_ranges`` 在 ``job.result``
    里（供前端与排障读取）。断言失败区间必须读 job.result，不能读返回值。
    """
    from app.models import Job
    job = session.get(Job, job_id)
    session.refresh(job)
    return (job.result or {}).get("failed_ranges") or []


# ---------------------------------------------------------------- 场景一

def test_gap_episode_markers_do_not_fall_back_to_full_text(tmp_path, monkeypatch):
    """集标记不连续（1/3/5，大纲 3 集）：缺失的第 2 批必须失败，绝不发全文。"""
    script = (
        "人物表：林昭——女主\n\n"
        "第1集 开端\n场次1 夜\n林昭：GAP1_第一集台词。\n\n"
        "第3集 转折\n场次1 夜\n林昭：GAP3_第三集台词。\n\n"
        "第5集 结局\n场次1 夜\n林昭：GAP5_第五集台词。\n"
    )
    session, project, sv, job, maker = _make_env(tmp_path, monkeypatch, script, 3)
    import app.services.script_agent as sa
    result = sa.execute_script_agent_job(job.id)

    # 任何一次解析请求都不得串入其他集的独有字符串
    for prompt in _FakeClient.all_parse_prompts:
        body = _body_of(prompt)
        m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", prompt)
        assert m
        b_start = int(m.group(1))
        own = f"GAP{b_start}_"
        others = [f"GAP{i}_" for i in (1, 3, 5) if i != b_start]
        assert own in body, f"第{b_start}批没拿到自己的正文"
        for o in others:
            assert o not in body, f"第{b_start}批串入了 {o}（疑似回退全文）"

    # 第 2 批必须标记失败（失败区间在 job.result，不在返回值）
    failed = _failed_ranges(session, job.id)
    assert "2-2" in failed, f"缺失集号批次未标记失败: {failed}"

    # chunking 事件要如实报出缺失集号
    ev = _chunking_event(session, job.id)
    assert 2 in ev.get("missing_episodes", []), ev

    session.close()


# ---------------------------------------------------------------- 场景二

def test_missing_episodes_are_not_padded_with_mismatched_text(tmp_path, monkeypatch):
    """标记数少于大纲集数：缺失集不得被 fallback 近似段填充（会张冠李戴）。

    剧本只有第1/2集标记，大纲 4 集。旧实现补齐的「第3集」实际是第2集正文，
    导致第2集剧情被当成第3集重复解析。
    """
    script = (
        "人物表：林昭\n\n"
        "第1集 开端\n场次1\n林昭：SHORT1_台词。\n\n"
        "第2集 发展\n场次1\n林昭：SHORT2_台词。\n"
    )
    session, project, sv, job, maker = _make_env(tmp_path, monkeypatch, script, 4)
    import app.services.script_agent as sa
    result = sa.execute_script_agent_job(job.id)

    # 核心断言：第3集绝不能拿到第2集的正文
    for prompt in _FakeClient.all_parse_prompts:
        body = _body_of(prompt)
        m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", prompt)
        assert m
        b_start = int(m.group(1))
        if b_start == 1:
            assert "SHORT1_" in body
            assert "SHORT2_" not in body, "第1批串入了第2集正文"
        elif b_start == 2:
            assert "SHORT2_" in body
            assert "SHORT1_" not in body, "第2批串入了第1集正文"
        else:
            # 第3/4集没有标记，本不该发起请求；万一发起也绝不能带别人的正文
            assert "SHORT1_" not in body and "SHORT2_" not in body, (
                f"第{b_start}批（缺失集）拿到了其他集的正文（fallback 张冠李戴）"
            )

    failed = _failed_ranges(session, job.id)
    assert any(r in failed for r in ("3-3", "4-4")), f"缺失集未标记失败: {failed}"

    ev = _chunking_event(session, job.id)
    assert 3 in ev.get("missing_episodes", []) and 4 in ev.get("missing_episodes", []), ev

    session.close()


# ---------------------------------------------------------------- 纯函数层

def test_slice_range_empty_is_detected():
    """确认两种场景下 slice_range 确实返回空串（修复的触发前提）。"""
    from app.services.script_chunker import slice_range, split_episodes

    gap = "第1集 A\n正文1\n\n第3集 B\n正文3\n\n第5集 C\n正文5\n"
    sp = split_episodes(gap)
    assert sp is not None
    assert [c.number for c in sp.chunks] == [1, 3, 5]
    assert slice_range(sp.chunks, 2, 2) == "", "缺失集号应返回空串"
    assert slice_range(sp.chunks, 1, 1).strip(), "存在的集号应返回正文"


def test_fallback_split_does_not_align_with_episode_numbers():
    """记录 fallback_split 的近似性：它的段号与剧本真实集号无关。

    这条测试是「禁止用 fallback 补齐缺失集」的依据——它证明补齐必然错位。
    """
    from app.services.script_chunker import fallback_split

    short = ("人物表：林昭\n\n第1集 开端\n场次1\n林昭：SHORT1_台词。\n\n"
             "第2集 发展\n场次1\n林昭：SHORT2_台词。\n")
    fb = fallback_split(short, 4)
    assert len(fb) == 4
    # 第3段装的是第2集正文 —— 证明段号 ≠ 集号
    assert "SHORT2_" in fb[2].text, "fallback 第3段应（错误地）落在第2集正文上"
    assert "SHORT2_" not in fb[0].text
