"""阶段 1–3 端到端验收：30 集、整本超预算的剧本。

这是原计划里缺失的『真实超长剧本』验收：前面的用例都用 3~5 集小剧本，
只验证了逻辑分支，没验证「整本放不下、单集放得下」这个真实触发条件。

验收点：
1. 预检判定整本超预算但单集放得下 → outline_mode=segmented，任务不被拒
2. 大纲自动走分段：多个窗口、每个窗口正文都不超预算
3. 合并后集号 1..30 连续，总集数正确
4. 解析严格按集切片：每个 prompt 只含本集独有标记，且长度远小于全文
5. 每个解析 prompt 正文都在预算内（不会再度触发上下文超限）
6. 30 集记录全部 succeeded，job.result 无 failed_ranges
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, ScriptVersion, ids

TOTAL_EPISODES = 30


def _build_script(n: int) -> str:
    head = "人物表：\n林昭——女主\n\n"
    body = "".join(
        f"第{i}集 标题{i}\n"
        f"场次1 夜 场景{i}\n"
        f"林昭：EZ{i}_第{i}集的台词内容。\n\n"
        for i in range(1, n + 1)
    )
    return head + body


SCRIPT_30 = _build_script(TOTAL_EPISODES)


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


def _outline_segment(window_episodes: int) -> dict:
    """分段大纲：只返回本窗口的 episodes（集号段内从 1 开始）。"""
    return {
        "total_episodes": window_episodes,
        "dialogue_char_estimate": 100 * window_episodes,
        "episodes": [
            {"episode": i, "title": f"段内{i}",
             "estimated_duration": 90, "estimated_shots": 2}
            for i in range(1, window_episodes + 1)
        ],
    }


def _parse_output(b_start: int, b_end: int) -> dict:
    scenes, eps = [], []
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
    """替身：大纲按『本段共 N 集』返回 N 条，解析按集范围返回，并记录所有提示词。"""

    all_outline_prompts: list[str] = []
    all_parse_prompts: list[str] = []
    all_eval_prompts: list[str] = []

    def __init__(self, *args, **kwargs):
        self.max_tokens = 32768
        self.last_metadata = None

    def _meta(self, chars: int) -> None:
        self.last_metadata = _Meta(chars)

    def chat_json(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        self._meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            raise AssertionError("解析不应走 chat_json")
        # 评估与大纲都走 chat_json：必须按特征分流，
        # 否则评估会收到大纲 JSON 而因缺字段被软闸口静默跳过。
        if "全面质量评估" in user_prompt:
            FakeLLMClient.all_eval_prompts.append(user_prompt)
            return {
                "strengths": [], "issues": [], "suggestions": [],
                "production_risks": [], "plot_holes": [],
                "overall_score": 8, "summary": "测试评估",
            }
        FakeLLMClient.all_outline_prompts.append(user_prompt)
        n = len(re.findall(r"^第\d+集", user_prompt, flags=re.M)) or 1
        return _outline_segment(n)

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self.chat_stream(system_prompt, user_prompt, json_mode=json_mode,
                                max_tokens=max_tokens)

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None):
        self._meta(len(user_prompt))
        if "请解析以下剧本" in user_prompt:
            FakeLLMClient.all_parse_prompts.append(user_prompt)
            m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", user_prompt)
            assert m, "解析 prompt 缺少集范围"
            payload = json.dumps(
                _parse_output(int(m.group(1)), int(m.group(2))), ensure_ascii=False)
            if on_delta is not None:
                on_delta(payload)
            return payload
        # 评估调用（流式路径）：结构合法即可（超预算时会走抽样）
        FakeLLMClient.all_eval_prompts.append(user_prompt)
        body = json.dumps({
            "strengths": [], "issues": [], "suggestions": [],
            "production_risks": [], "plot_holes": [],
            "overall_score": 8, "summary": "测试评估",
        }, ensure_ascii=False)
        if on_delta is not None:
            on_delta(body)
        return body


class _Settings:
    """窗口压到 1000 token：整本 30 集必然超预算，单集仍放得下。"""

    max_episodes_per_batch = 1
    agent_first_batch_auto = False
    llm_parse_concurrency = 2
    llm_parse_retry_limit = 0
    llm_max_tokens = 32768
    llm_parse_max_tokens = 65536
    llm_parse_hard_max_tokens = 131072
    llm_timeout_seconds = 30
    llm_stream_idle_timeout_seconds = 30
    llm_context_window_tokens = 1000
    llm_input_budget_ratio = 0.6
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3
    parse_shot_ratio_warn = 0.5


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'long_script.db'}")
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

    FakeLLMClient.all_outline_prompts.clear()
    FakeLLMClient.all_parse_prompts.clear()
    FakeLLMClient.all_eval_prompts.clear()

    session = maker()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="超长剧本项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot=SCRIPT_30, content_hash="hash_long_30", created_at=now,
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
    import app.services.script_agent as sa

    result = sa.execute_script_agent_job(job.id)
    return maker, project, job, result


def _events(session, job_id: str, event_type: str) -> list:
    from app.models import JobEvent

    return (
        session.query(JobEvent)
        .filter(JobEvent.job_id == job_id, JobEvent.event_type == event_type)
        .all()
    )


def test_precheck_marks_outline_mode_segmented(run_job):
    """整本超预算、单集放得下 → 预检放行并标记 segmented。"""
    maker, project, job, result = run_job
    assert result.get("ok") is True

    session = maker()
    try:
        evs = _events(session, job.id, "agent.input_precheck")
        assert len(evs) == 1
        payload = evs[0].payload or {}
        assert payload.get("ok") is True
        assert payload.get("outline_mode") == "segmented"
        assert payload.get("budget") == 600
        assert payload.get("estimated_tokens") > payload.get("budget")
    finally:
        session.close()


def test_outline_runs_in_multiple_windows(run_job):
    """大纲必须分段执行：多个窗口，而不是整本一次送出。"""
    maker, project, job, result = run_job
    outline_prompts = [p for p in FakeLLMClient.all_outline_prompts
                       if "请分析以下剧本的分集结构" in p]
    assert len(outline_prompts) >= 2, "整本超预算却没有分段"
    # 每个窗口都带本段集号说明，且正文远小于全文
    for p in outline_prompts:
        assert "本段从第" in p
        body = p.split("请分析以下剧本的分集结构：", 1)[1]
        assert len(body) < len(SCRIPT_30) * 0.6
    # 窗口覆盖：拼起来应覆盖全部 30 集标记
    total_markers = sum(len(re.findall(r"^第\d+集", p, flags=re.M))
                        for p in outline_prompts)
    assert total_markers == TOTAL_EPISODES


def test_outline_merges_with_continuous_episode_numbers(run_job):
    """合并后集号 1..30 连续，无重复无断档。"""
    maker, project, job, result = run_job

    session = maker()
    try:
        row = session.query(Job).filter(Job.id == job.id).one()
        evs = _events(session, job.id, "agent.precheck")
        assert len(evs) == 1
        assert (evs[0].payload or {}).get("estimated_episodes") == TOTAL_EPISODES
        assert (evs[0].payload or {}).get("outline_mode") == "segmented"
        assert row.status == "succeeded"
    finally:
        session.close()


def test_parse_prompts_cover_each_episode_once(run_job):
    """解析按集切片：30 批，每批只含本集独有标记。"""
    maker, project, job, result = run_job
    assert len(FakeLLMClient.all_parse_prompts) == TOTAL_EPISODES

    for prompt in FakeLLMClient.all_parse_prompts:
        m = re.search(r"仅解析第 (\d+) 集到第 (\d+) 集", prompt)
        assert m
        b_start, b_end = int(m.group(1)), int(m.group(2))
        assert b_start == b_end  # max_episodes_per_batch=1
        assert f"EZ{b_start}_" in prompt
        # 其他集的独有标记绝不能出现在本批提示词里
        for other in range(1, TOTAL_EPISODES + 1):
            if other != b_start:
                assert f"EZ{other}_" not in prompt


def test_each_parse_prompt_within_budget(run_job):
    """每个解析提示词正文都在输入预算内，不会再度触发上下文超限。"""
    from app.integrations.token_estimate import estimate_input_tokens

    maker, project, job, result = run_job
    settings = _Settings()
    budget = int(settings.llm_context_window_tokens * settings.llm_input_budget_ratio)
    for prompt in FakeLLMClient.all_parse_prompts:
        body = prompt.split("请解析以下剧本", 1)[1]
        assert estimate_input_tokens(body, settings=settings) <= budget


def test_all_episode_records_succeeded(run_job):
    """30 集记录全部 succeeded，且 job.result 无失败区间。"""
    from app.models import EpisodeParseRecord

    maker, project, job, result = run_job
    session = maker()
    try:
        rows = (
            session.query(EpisodeParseRecord)
            .filter(EpisodeParseRecord.project_id == project.id,
                    EpisodeParseRecord.asset_type == "all")
            .order_by(EpisodeParseRecord.episode_no)
            .all()
        )
        assert [r.episode_no for r in rows] == list(range(1, TOTAL_EPISODES + 1))
        assert {r.status for r in rows} == {"succeeded"}
        # 每集 2 镜
        assert {r.asset_count for r in rows} == {2}

        row = session.query(Job).filter(Job.id == job.id).one()
        assert (row.result or {}).get("failed_ranges") == []
        assert (row.result or {}).get("total_episodes") == TOTAL_EPISODES
    finally:
        session.close()


def test_evaluation_is_sampled_for_long_script(run_job):
    """整本超预算时评估走抽样，并显式标记范围与抽样集。"""
    maker, project, job, result = run_job
    session = maker()
    try:
        row = session.query(Job).filter(Job.id == job.id).one()
        evaluation = (row.result or {}).get("evaluation") or {}
        assert evaluation.get("evaluation_scope") == "sampled"
        picked = evaluation.get("sampled_episodes") or []
        assert 1 in picked and TOTAL_EPISODES in picked
        assert len(picked) < TOTAL_EPISODES
    finally:
        session.close()
