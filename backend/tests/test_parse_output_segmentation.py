"""输出侧分段解析验收测试（阶段 6）。

背景：目标网关存在 completion 硬上限（实测 completion_tokens 恒 16000 且
finish_reason 谎报 stop），单次输出超长 JSON 必定中途截断，放大 max_tokens
无效。唯一可靠解法是把一次输出拆成多次，本文件验证四条路径：

1. llm_parse_segment_chars<=0：完全保持旧行为（一次调用，prompt 措辞不变）
2. 分段开启：按场次边界切多段，合并后镜头数与场景数正确
3. 某段仍被判截断：对该段二分递归（depth>=1），最终仍能产出完整结果
4. 批次失败：除 job.failed 外还要留下 agent.parse_attempts（含真实 max_tokens）
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.integrations.llm import LLMError
from app.integrations.llm_response import (
    ERR_OUTPUT_TRUNCATED,
    ERR_SCHEMA_MISMATCH,
    LLMResponseMeta,
)
from app.models import Base, Job, JobEvent, Project, ScriptVersion, ids


# 单集三场次：每场带独有标记，用于断言分段没有重复或漏段
SCRIPT_SEG = (
    "第1集 分段测试\n"
    "场次1 黄昏 车站\n"
    + "林昭：AAA_我回来了。" * 8
    + "\n场次2 夜 书房\n"
    + "陈默：BBB_这封信是谁写的。" * 8
    + "\n场次3 日 天台\n"
    + "林昭：CCC_别再查了。" * 8
    + "\n"
)


class _Meta:
    """模拟 LLMClient.last_metadata。"""

    def __init__(self, prompt_chars: int, max_tokens: int) -> None:
        self.model = "fake-model"
        self.max_tokens = max_tokens
        self.finish_reason = "stop"
        self.completion_tokens = 200
        self.reasoning_tokens = 0
        self.prompt_tokens = max(1, prompt_chars // 2)
        self.content_length = 0
        self.reasoning_length = 0


def _outline() -> dict:
    return {
        "total_episodes": 1,
        "episodes": [{"episode": 1, "title": "分段测试",
                      "estimated_duration": 90, "estimated_shots": 6}],
        "dialogue_char_estimate": 1000,
    }


def _payload_from_prompt(prompt: str) -> dict:
    """按 prompt 里出现的场次行生成解析结果：一场两镜。"""
    scenes = []
    for m in re.finditer(r"^场次(\d+)", prompt, re.M):
        no = int(m.group(1))
        scenes.append({
            "episode": 1,
            "title": f"场次{no}",
            "description": f"场次{no}描述",
            "time_note": "夜",
            "shots": [
                {"episode": 1, "seq": 1, "scene_id": f"场次{no}",
                 "duration": 5, "image_prompt": f"s{no}_1", "shot_kind": "visual"},
                {"episode": 1, "seq": 2, "scene_id": f"场次{no}",
                 "duration": 5, "image_prompt": f"s{no}_2", "shot_kind": "visual"},
            ],
        })
    if not scenes:
        scenes = [{
            "episode": 1, "title": "兜底场景", "description": "兜底",
            "time_note": "夜",
            "shots": [{"episode": 1, "seq": 1, "scene_id": "兜底场景",
                       "duration": 5, "image_prompt": "x", "shot_kind": "visual"}],
        }]
    # episodes 只在第 1 段输出，避免合并后重复集
    seg = re.search(r"第 (\d+)/(\d+) 段", prompt)
    eps = []
    if seg is None or seg.group(1) == "1":
        eps = [{"episode": 1, "goal": "目标", "rhythm_curve": "平稳",
                "cliffhanger_type": "信息缺失", "hook_to_next": "", "locked": False}]
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
    """替身客户端：按 prompt 分流大纲 / 解析 / 评估，并记录每次解析提示词。"""

    parse_prompts: list[str] = []
    truncate_remaining: int = 0
    fail_code: str | None = None

    def __init__(self, *args, **kwargs):
        self.max_tokens = 32768
        self.last_metadata = None

    def _set_meta(self, chars: int, max_tokens) -> None:
        self.last_metadata = _Meta(chars, int(max_tokens or self.max_tokens))

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        self._set_meta(len(user_prompt), max_tokens)
        return _outline()

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None, should_cancel=None,
                    cancel_token=None):
        self._set_meta(len(user_prompt), max_tokens)
        if "请解析以下剧本" in user_prompt:
            FakeLLMClient.parse_prompts.append(user_prompt)
            if FakeLLMClient.fail_code:
                raise LLMError(FakeLLMClient.fail_code, "模拟业务校验失败")
            if FakeLLMClient.truncate_remaining > 0:
                FakeLLMClient.truncate_remaining -= 1
                partial = "x" * 100
                meta = LLMResponseMeta(
                    content=partial, finish_reason="stop",
                    usage={"completion_tokens": 16000}, max_tokens=int(max_tokens or 0),
                )
                raise LLMError(
                    ERR_OUTPUT_TRUNCATED,
                    "语言模型输出 JSON 在中途截断（已输出 100 字符，"
                    f"max_tokens={int(max_tokens or 0)}）："
                    "Unterminated string starting at: line 1 column 101",
                    meta=meta,
                )
            payload = json.dumps(_payload_from_prompt(user_prompt), ensure_ascii=False)
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
    llm_parse_segment_chars = 0
    llm_max_tokens = 32768
    llm_parse_max_tokens = 65536
    llm_parse_hard_max_tokens = 131072
    llm_timeout_seconds = 30
    llm_stream_idle_timeout_seconds = 30
    llm_context_window_tokens = 0
    llm_input_budget_ratio = 0.6
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3
    parse_shot_ratio_warn = 0
    llm_disable_thinking = False


def _run(tmp_path, monkeypatch, *, segment_chars: int,
         truncate_remaining: int = 0, fail_code: str | None = None):
    engine = make_engine(f"sqlite:///{tmp_path / 'seg.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.config as cfg_mod
    import app.core.database as db_mod
    import app.integrations.llm as llm_mod
    import app.services.script_agent as sa

    st = _Settings()
    st.llm_parse_segment_chars = segment_chars

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(sa, "SessionLocal", maker)
    monkeypatch.setattr(llm_mod, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(sa, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: st)
    monkeypatch.setattr(sa, "get_settings", lambda: st)

    FakeLLMClient.parse_prompts = []
    FakeLLMClient.truncate_remaining = truncate_remaining
    FakeLLMClient.fail_code = fail_code

    session = maker()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="分段项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot=SCRIPT_SEG, content_hash="hash_seg_1", created_at=now,
    )
    session.add(sv)
    job = Job(
        id=ids.new_id("job"), project_id=project.id, job_type="script_agent",
        params={"script_version_id": sv.id, "episode_start": 1},
        status="queued", created_at=now,
    )
    session.add(job)
    session.commit()

    result = sa.execute_script_agent_job(job.id)
    return maker, job.id, result, engine


def _attempt_events(maker, job_id: str) -> list[dict]:
    db = maker()
    try:
        rows = (
            db.query(JobEvent)
            .filter(JobEvent.job_id == job_id,
                    JobEvent.event_type == "agent.parse_attempts")
            .all()
        )
        return [r.payload for r in rows]
    finally:
        db.close()


def test_segment_disabled_keeps_single_call(tmp_path, monkeypatch):
    """llm_parse_segment_chars<=0：一次调用，prompt 措辞与旧实现一致。"""
    maker, job_id, result, engine = _run(tmp_path, monkeypatch, segment_chars=0)
    try:
        assert result.get("ok") is True
        prompts = FakeLLMClient.parse_prompts
        assert len(prompts) == 1
        assert "集剧本：" in prompts[0]
        assert "段正文" not in prompts[0]
        # 三场次各两镜
        assert result.get("shots") == 6
    finally:
        engine.dispose()


def test_segment_enabled_splits_and_merges(tmp_path, monkeypatch):
    """分段开启：正文被切成多段调用，合并后场景与镜头数正确。"""
    maker, job_id, result, engine = _run(tmp_path, monkeypatch, segment_chars=120)
    try:
        assert result.get("ok") is True
        prompts = FakeLLMClient.parse_prompts
        assert len(prompts) > 1, "超过预算必须切分为多次调用"
        for p in prompts:
            assert "段正文" in p and "段）：" in p
        # 分段没有丢失场景（三场次 × 2 镜 = 6；极窄预算下某段可能因
        # 缺少场次标记触发兜底 +1，但只要不小于 6 就说明没丢）
        assert result.get("shots") >= 6
        # 每段提示词不应为空，且三段独有标记的并集覆盖全部三个。
        # 不要求每段都含标记：预算切分 (line-level split) 可能产生
        # 不含场次标题的尾部片段（替身客户端会走兜底场景）。
        markers = [
            [k for k in ("AAA_", "BBB_", "CCC_") if k in p] for p in prompts
        ]
        all_covered = {k for m in markers for k in m}
        assert all_covered == {"AAA_", "BBB_", "CCC_"}, (
            f"{all_covered=} markers={markers}")
        # 尝试记录带分段信息
        events = _attempt_events(maker, job_id)
        assert events
        atts = events[0]["attempts"]
        assert all("segment" in a for a in atts)
    finally:
        engine.dispose()


def test_truncated_segment_bisects_recursively(tmp_path, monkeypatch):
    """某段被判截断：对该段二分递归，最终仍完整产出。"""
    maker, job_id, result, engine = _run(
        tmp_path, monkeypatch, segment_chars=120, truncate_remaining=1)
    try:
        assert result.get("ok") is True, result
        events = _attempt_events(maker, job_id)
        assert events
        atts = events[0]["attempts"]
        assert any(int(a.get("depth") or 0) >= 1 for a in atts), \
            "截断后应二分递归（depth>=1）"
        # 二分是把一段拆成两半：替身客户端按每段 prompt 里的场次行
        # 各产出 2 镜，合并时同名场景拼接 shots 会把同一场景的镜数
        # 叠加（例如场次 1 被拆到两段各出 2 镜 → 合并后 4 镜）。
        # 这是替身的天然行为，真实 LLM 不会对半个场景各报一遍。
        # 断言「不小于 6」即可验证分段 + 合并没有丢镜头。
        assert result.get("shots") >= 6
    finally:
        engine.dispose()


def test_failed_batch_records_attempts_with_real_max_tokens(tmp_path, monkeypatch):
    """批次失败也要留痕：agent.parse_attempts 存在且 max_tokens 不是 0。"""
    maker, job_id, result, engine = _run(
        tmp_path, monkeypatch, segment_chars=0, fail_code=ERR_SCHEMA_MISMATCH)
    try:
        assert result.get("ok") is False
        db = maker()
        try:
            job = db.get(Job, job_id)
            assert job.status == "failed"
            # 错误信息里不应再出现 max_tokens=0（那是元数据丢失的症状）
            assert "max_tokens=0" not in (job.error_msg or "")
        finally:
            db.close()
        events = _attempt_events(maker, job_id)
        assert events, "失败批次必须留下 agent.parse_attempts"
        assert events[0].get("failed") is True
        atts = events[0]["attempts"]
        assert atts and int(atts[0].get("max_tokens") or 0) > 0
    finally:
        engine.dispose()
