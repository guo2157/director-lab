"""阶段 5 测试：多批部分失败时的集级状态不应被覆盖为 succeeded。

复现的缺陷（旧行为）：
1. 第 2 批解析失败 → `_upsert_records(..., "failed")` 把该集标成 failed（:464）
2. 部分失败分支只 `db.commit()`，没有 return（:474）
3. 流程继续走到最终落库（:601），对**整个 batch 范围**写 succeeded
   → 第 2 集的 failed 被覆盖成 succeeded，前端看不到失败集

期望：
- 失败集记录保持 failed，成功集为 succeeded
- job.result.failed_ranges 明确列出失败区间
- 任务整体仍为 succeeded（部分成功语义），但结果里不再谎报全部成功
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.integrations.llm import LLMError
from app.models import Base, Job, Project, ScriptVersion, ids

SCRIPT_3 = """人物表：
林昭——女主

第1集 归途
场次1 黄昏 车站
林昭：EZ1_我回来了。

第2集 旧信
场次1 夜 书房
林昭：EZ2_这封信是谁写的？

第3集 落幕
场次1 黎明 海边
林昭：EZ3_结束了。
"""

# 需要持续抛错的集号（模拟该批重试耗尽仍失败）
FAIL_EPISODES: set[int] = {2}


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


def _outline() -> dict:
    return {
        "total_episodes": 3,
        "estimated_total_duration": 270,
        "dialogue_char_estimate": 600,
        "episodes": [
            {"episode": i, "title": f"标题{i}",
             "estimated_duration": 90, "estimated_shots": 2}
            for i in (1, 2, 3)
        ],
    }


def _parse_output(ep: int) -> dict:
    return {
        "project_analysis": {"summary": "测试", "theme": "测试",
                             "highlights": [], "foreshadowing": []},
        "characters": [{"name": "林昭", "description": "女主",
                        "traits": "", "appearance": ""}],
        "props": [],
        "scenes": [{
            "episode": ep,
            "title": f"第{ep}集场景",
            "description": f"第{ep}集场景描述",
            "time_note": "夜",
            "shots": [
                {"episode": ep, "seq": n + 1, "scene_id": f"第{ep}集场景",
                 "duration": 5, "image_prompt": f"ep{ep} 镜{n + 1}",
                 "shot_kind": "visual"}
                for n in range(2)
            ],
        }],
        "episodes": [{
            "episode": ep, "goal": f"目标{ep}", "rhythm_curve": "平稳",
            "cliffhanger_type": "信息缺失", "hook_to_next": "", "locked": False,
        }],
    }


def _batch_range(user_prompt: str) -> tuple[int, int]:
    m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
    assert m, "解析 prompt 缺少集范围"
    return int(m.group(1)), int(m.group(2))


class FakeLLMClient:
    """替身：FAIL_EPISODES 中的集在解析与流式两条路径都抛错。"""

    def __init__(self, *args, **kwargs):
        self.max_tokens = 32768
        self.last_metadata = None

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        self.last_metadata = _Meta(len(user_prompt))
        if "请解析以下剧本" not in user_prompt:
            return _outline()
        b_start, _ = _batch_range(user_prompt)
        if b_start in FAIL_EPISODES:
            raise LLMError("llm_provider_error",
                           f"HTTP 500: 第{b_start}集解析持续失败")
        return _parse_output(b_start)

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None):
        self.last_metadata = _Meta(len(user_prompt))
        if "请解析以下剧本" not in user_prompt:
            payload = json.dumps(_outline(), ensure_ascii=False)
        else:
            b_start, _ = _batch_range(user_prompt)
            if b_start in FAIL_EPISODES:
                raise LLMError("llm_provider_error",
                               f"HTTP 500: 第{b_start}集解析持续失败")
            payload = json.dumps(_parse_output(b_start), ensure_ascii=False)
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
    llm_context_window_tokens = 0
    llm_input_budget_ratio = 0.6
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3
    parse_shot_ratio_warn = 0  # 关闭镜头对账，聚焦状态机


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'partial.db'}")
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
        id=ids.project_id(), name="部分失败项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot=SCRIPT_3, content_hash="hash_partial_3", created_at=now,
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


def test_failed_episode_record_not_overwritten_as_succeeded(env):
    """第 2 批失败时，第 2 集记录必须保持 failed，不能被最终落库刷成 succeeded。"""
    session, project, sv, job, maker = env
    import app.services.script_agent as sa

    sa.execute_script_agent_job(job.id)

    from app.models import EpisodeParseRecord

    check = maker()
    try:
        rows = (
            check.query(EpisodeParseRecord)
            .filter(EpisodeParseRecord.project_id == project.id,
                    EpisodeParseRecord.asset_type == "all")
            .order_by(EpisodeParseRecord.episode_no)
            .all()
        )
        status_by_ep = {r.episode_no: r.status for r in rows}
        assert status_by_ep.get(1) == "succeeded"
        assert status_by_ep.get(2) == "failed", (
            f"第 2 集应为 failed，实际 {status_by_ep.get(2)}")
        assert status_by_ep.get(3) == "succeeded"
        # 失败集应带错误信息
        row2 = next(r for r in rows if r.episode_no == 2)
        assert row2.error_msg
    finally:
        check.close()


def test_partial_failure_result_lists_failed_ranges(env):
    """job.result 必须显式列出失败区间，避免前端误以为全部成功。"""
    session, project, sv, job, maker = env
    import app.services.script_agent as sa

    sa.execute_script_agent_job(job.id)

    check = maker()
    try:
        row = check.query(Job).filter(Job.id == job.id).one()
        result = row.result or {}
        assert result.get("failed_ranges") == ["2-2"], (
            f"failed_ranges 应为 ['2-2']，实际 {result.get('failed_ranges')}")
        # 部分成功：任务整体仍为 succeeded，但结果里带失败信息
        assert row.status == "succeeded"
    finally:
        check.close()


def test_success_asset_counts_not_written_to_failed_episode(env):
    """成功批次的 asset_counts 不应写到失败集上（否则失败集也显示有镜头）。"""
    session, project, sv, job, maker = env
    import app.services.script_agent as sa

    sa.execute_script_agent_job(job.id)

    from app.models import EpisodeParseRecord

    check = maker()
    try:
        row2 = (
            check.query(EpisodeParseRecord)
            .filter(EpisodeParseRecord.project_id == project.id,
                    EpisodeParseRecord.episode_no == 2,
                    EpisodeParseRecord.asset_type == "all")
            .one()
        )
        assert row2.status == "failed"
        assert (row2.asset_count or 0) == 0
    finally:
        check.close()


def test_all_batches_succeed_no_failed_ranges(env):
    """对照组：全部成功时 failed_ranges 为空，三集都是 succeeded。"""
    session, project, sv, job, maker = env
    FAIL_EPISODES.clear()
    import app.services.script_agent as sa

    sa.execute_script_agent_job(job.id)

    from app.models import EpisodeParseRecord

    check = maker()
    try:
        rows = (
            check.query(EpisodeParseRecord)
            .filter(EpisodeParseRecord.project_id == project.id,
                    EpisodeParseRecord.asset_type == "all")
            .all()
        )
        assert {r.status for r in rows} == {"succeeded"}
        row = check.query(Job).filter(Job.id == job.id).one()
        assert (row.result or {}).get("failed_ranges") == []
    finally:
        check.close()
        FAIL_EPISODES.add(2)
