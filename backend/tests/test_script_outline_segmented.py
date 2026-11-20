"""阶段 3 测试：分段大纲与评估抽样。

覆盖：
- pack_windows：按预算打包、不切断单集、预算为 0 时单窗口
- outline_user_prompt：窗口说明只加在 user prompt
- build_outline_payload：集号连续、字段与整本路径一致
- analyze_outline_segmented：多窗口合并集号 1..N 连续、对白字数汇总、
  无集标记且超预算抛 llm_input_too_long
- analyze_script_outline：未显式指定时按预算自动分段；未超预算只发 1 次请求
- _sample_episodes_for_evaluation：≤6 集全取；>6 集取首 2 + 中间等距 2 + 末 1
- evaluate_script：超预算 scope=sampled 且送抽样本；未超预算 scope=full 送全文
"""
from __future__ import annotations

import re

import pytest

from app.services.script_evaluation import _sample_episodes_for_evaluation
from app.services.script_outline_segmented import (
    analyze_outline_segmented,
    build_outline_payload,
    outline_user_prompt,
    pack_windows,
)
from app.services.script_parse import analyze_script_outline


def _script(n: int) -> str:
    head = "人物表：\n林昭——女主\n\n"
    body = "".join(
        f"第{i}集 标题{i}\n场次1 夜 场景{i}\n林昭：EZ{i}_第{i}集台词内容。\n\n"
        for i in range(1, n + 1)
    )
    return head + body


class _Chunk:
    def __init__(self, number: int, text: str, title: str = ""):
        self.number = number
        self.text = text
        self.title = title


class _Settings:
    """最小配置桩：只提供预检与并发相关字段。"""

    def __init__(self, *, window=100000, ratio=0.6, cjk=1.2, other=0.3, concurrency=1):
        self.llm_context_window_tokens = window
        self.llm_input_budget_ratio = ratio
        self.llm_cjk_token_factor = cjk
        self.llm_other_token_factor = other
        self.llm_parse_concurrency = concurrency


class _Meta:
    def __init__(self):
        self.model = "fake"
        self.max_tokens = 0
        self.finish_reason = "stop"
        self.completion_tokens = 100
        self.reasoning_tokens = 0
        self.prompt_tokens = 500
        self.content_length = 100
        self.reasoning_length = 0


class _OutlineClient:
    """替身大纲客户端：按 user prompt 里的『共 N 集』返回 N 条 episodes。"""

    def __init__(self):
        self.max_tokens = 32768
        self.last_metadata = None
        self.prompts: list[str] = []

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.prompts.append(user_prompt)
        self.last_metadata = _Meta()
        m = re.search(r"共 (\d+) 集", user_prompt)
        n = int(m.group(1)) if m else 1
        start = re.search(r"本段从第 (\d+) 集开始", user_prompt)
        base = int(start.group(1)) if start else 1
        return {
            "total_episodes": 1,  # 分段路径不依赖它
            "dialogue_char_estimate": 100 * n,
            "episodes": [
                {
                    "episode": i + 1,
                    "title": f"段内第{i + 1}集",
                    "estimated_duration": 60,
                    "estimated_shots": 6,
                }
                for i in range(n)
            ],
            "_base": base,
        }


class _SingleShotClient:
    """整本路径替身：记录调用次数并返回合法大纲。"""

    def __init__(self, total=3):
        self.max_tokens = 32768
        self.last_metadata = None
        self.calls = 0
        self.total = total

    def chat_json(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        self.last_metadata = _Meta()
        return {
            "total_episodes": self.total,
            "dialogue_char_estimate": 999,
            "episodes": [
                {"episode": i, "title": f"整本第{i}集",
                 "estimated_duration": 90, "estimated_shots": 9}
                for i in range(1, self.total + 1)
            ],
        }


# ============================================ pack_windows
def test_pack_windows_keeps_episode_intact():
    chunks = [_Chunk(i, "字" * 50) for i in range(1, 7)]
    s = _Settings(window=100, ratio=0.6)  # 预算 60 token，单集 60 token
    windows = pack_windows(chunks, settings=s)
    assert len(windows) >= 2
    # 每个 chunk 只出现在一个窗口，且不被拆开
    flat = [c.number for w in windows for c in w]
    assert flat == [1, 2, 3, 4, 5, 6]


def test_pack_windows_single_window_when_budget_unknown():
    chunks = [_Chunk(i, "字" * 100) for i in range(1, 6)]
    windows = pack_windows(chunks, settings=_Settings(window=0))
    assert len(windows) == 1
    assert len(windows[0]) == 5


def test_pack_windows_single_window_when_within_budget():
    chunks = [_Chunk(i, "短") for i in range(1, 4)]
    windows = pack_windows(chunks, settings=_Settings(window=100000))
    assert len(windows) == 1


def test_pack_windows_never_drops_chunks():
    chunks = [_Chunk(i, "字" * 30) for i in range(1, 11)]
    windows = pack_windows(chunks, settings=_Settings(window=100, ratio=0.6))
    assert sum(len(w) for w in windows) == 10


# ============================================ prompt

def test_outline_user_prompt_includes_window_note():
    p = outline_user_prompt("正文", 90, window_note="本段从第 3 集开始，共 2 集。")
    assert "本段从第 3 集开始" in p
    assert "正文" in p
    assert "目标单集时长：90" in p


def test_outline_user_prompt_without_note():
    p = outline_user_prompt("正文", 90)
    assert "本段从第" not in p
    assert "正文" in p


# ============================================ build_outline_payload

def test_build_outline_payload_renumbers_and_sums():
    breakdown = [
        {"episode": 1, "title": "a", "estimated_duration": 30.0, "estimated_shots": 3},
        {"episode": 2, "title": "b", "estimated_duration": 45.5, "estimated_shots": 5},
    ]
    out = build_outline_payload(
        "x" * 100, algorithm_version="v-test", max_episodes_per_batch=1,
        breakdown=breakdown, dialogue_chars=777,
    )
    assert out["estimated_episodes"] == 2
    assert out["estimated_total_duration"] == 75.5
    assert out["char_count"] == 100
    assert out["dialogue_char_estimate"] == 777
    assert out["algorithm_version"] == "v-test"
    assert out["exceeds_batch_limit"] is True
    assert "2 集" in out["suggestion"]


def test_build_outline_payload_within_batch_limit_has_no_suggestion():
    out = build_outline_payload(
        "x", algorithm_version="v", max_episodes_per_batch=30,
        breakdown=[{"episode": 1, "title": "", "estimated_duration": 90.0,
                    "estimated_shots": 9}],
        dialogue_chars=0,
    )
    assert out["exceeds_batch_limit"] is False
    assert out["suggestion"] is None


# ============================================ 分段合并

def test_segmented_merges_with_continuous_episode_numbers():
    text = _script(20)
    client = _OutlineClient()
    # 预算很小 → 必然多窗口
    s = _Settings(window=40, ratio=0.6, concurrency=2)
    out = analyze_outline_segmented(
        text, episode_duration_target=90, max_episodes_per_batch=1,
        algorithm_version="v", avg_shot_duration=10.0,
        client=client, settings=s,
    )
    assert len(client.prompts) >= 2
    eps = [e["episode"] for e in out["episode_breakdown"]]
    assert eps == list(range(1, len(eps) + 1))
    # 合并后的集数不应少于分集标记数
    assert out["estimated_episodes"] == len(out["episode_breakdown"])
    assert out["dialogue_char_estimate"] > 0


def test_segmented_prompts_do_not_exceed_budget():
    """每个窗口的 prompt 正文都应在预算内（不切断单集 + 按预算打包）。"""
    from app.integrations.token_estimate import estimate_input_tokens

    text = _script(20)
    client = _OutlineClient()
    s = _Settings(window=400, ratio=0.6, concurrency=1)
    budget = int(400 * 0.6)
    analyze_outline_segmented(
        text, episode_duration_target=90, max_episodes_per_batch=1,
        algorithm_version="v", avg_shot_duration=10.0,
        client=client, settings=s,
    )
    for prompt in client.prompts:
        body = prompt.split("请分析以下剧本的分集结构：", 1)[1]
        assert estimate_input_tokens(body, settings=s) <= budget


def test_segmented_without_markers_raises_input_too_long():
    from app.integrations.llm import LLMError
    from app.integrations.llm_response import ERR_INPUT_TOO_LONG

    with pytest.raises(LLMError) as ei:
        analyze_outline_segmented(
            "没有任何分集标记的一长段剧本。" * 100,
            episode_duration_target=90, max_episodes_per_batch=1,
            algorithm_version="v", avg_shot_duration=10.0,
            client=_OutlineClient(), settings=_Settings(window=100),
        )
    assert ei.value.code == ERR_INPUT_TOO_LONG
    assert "第X集" in ei.value.message


# ============================================ analyze_script_outline 集成

def test_outline_full_path_single_call_when_within_budget():
    """不超预算：只发 1 次请求，行为与改造前一致。"""
    client = _SingleShotClient(total=3)
    s = _Settings(window=1000000)
    out = analyze_script_outline(
        _script(3), episode_duration_target=90, max_episodes_per_batch=1,
        client=client, settings=s,
    )
    assert client.calls == 1
    assert out["estimated_episodes"] == 3
    assert out["algorithm_version"] == "llm-outline-v1.0"


def test_outline_auto_segments_when_over_budget():
    """未显式传 segmented 时按预算自动分段（estimate 端点依赖此兜底）。"""
    client = _OutlineClient()
    s = _Settings(window=60, ratio=0.6, concurrency=1)
    out = analyze_script_outline(
        _script(12), episode_duration_target=90, max_episodes_per_batch=1,
        client=client, settings=s,
    )
    assert len(client.prompts) >= 2
    assert out["estimated_episodes"] == len(out["episode_breakdown"])


def test_outline_explicit_segmented_false_still_auto_detects():
    """显式传 False 不是『禁止分段』，而是『让函数自己判断』。"""
    client = _OutlineClient()
    s = _Settings(window=60, ratio=0.6, concurrency=1)
    analyze_script_outline(
        _script(12), episode_duration_target=90, max_episodes_per_batch=1,
        client=client, settings=s, segmented=False,
    )
    assert len(client.prompts) >= 2


# ============================================ 评估抽样

def test_sample_all_when_six_or_fewer():
    text = _script(6)
    body, eps = _sample_episodes_for_evaluation(text)
    assert eps == [1, 2, 3, 4, 5, 6]
    for i in range(1, 7):
        assert f"EZ{i}_" in body


def test_sample_takes_first_middle_and_last():
    text = _script(20)
    body, eps = _sample_episodes_for_evaluation(text)
    assert len(eps) < 20
    assert 1 in eps and 2 in eps
    assert 20 in eps
    # 抽样集合有序且唯一
    assert eps == sorted(set(eps))
    # 人物表（序言）必须保留
    assert "人物表" in body
    # 被抽中的集在正文里，未抽中的不出现
    for i in eps:
        assert f"EZ{i}_" in body
    dropped = [i for i in range(1, 21) if i not in eps]
    for i in dropped:
        assert f"EZ{i}_" not in body


def test_sample_without_markers_returns_whole_text():
    text = "没有分集标记的一长段内容。" * 50
    body, eps = _sample_episodes_for_evaluation(text)
    assert body == text
    assert eps == []


# ============================================ evaluate_script 抽样开关
class _Report:
    """合法评估报告。"""

    @staticmethod
    def build():
        return {
            "strengths": [], "issues": [], "suggestions": [],
            "production_risks": [], "plot_holes": [],
            "overall_score": 8, "summary": "ok",
        }


def _run_evaluate(monkeypatch, text, settings):
    """替换 call_with_retry 与 get_prompt，捕获真正送出的 user prompt。"""
    captured = {}

    def _fake_retry(client, *, system_prompt, user_prompt, **kwargs):
        captured["user"] = user_prompt
        return _Report.build()

    monkeypatch.setattr(
        "app.integrations.llm_retry.call_with_retry", _fake_retry, raising=False)
    monkeypatch.setattr(
        "app.services.prompt_store.get_prompt", lambda name: "sys", raising=False)

    from app.services.script_evaluation import evaluate_script

    # estimate_data 必须是完整大纲结构：build_evaluation_user_prompt 会对
    # estimated_total_duration 做 .1f 格式化，缺字段会抛 ValueError（调用方保证）
    estimate = {
        "char_count": len(text),
        "dialogue_char_estimate": 1000,
        "estimated_total_duration": 90.0,
        "estimated_episodes": 3,
        "algorithm_version": "llm-outline-v1.0",
        "episode_breakdown": [
            {"episode": 1, "title": "a", "estimated_duration": 90.0, "estimated_shots": 9},
        ],
    }
    report = evaluate_script(
        text, estimate, {}, settings=settings)
    return report, captured["user"]


def test_evaluate_full_scope_when_within_budget(monkeypatch):
    text = _script(3)
    report, user = _run_evaluate(monkeypatch, text, _Settings(window=1000000))
    assert report["evaluation_scope"] == "full"
    assert report["sampled_episodes"] == []
    for i in range(1, 4):
        assert f"EZ{i}_" in user


def test_evaluate_sampled_scope_when_over_budget(monkeypatch):
    text = _script(20)
    report, user = _run_evaluate(monkeypatch, text, _Settings(window=60, ratio=0.6))
    assert report["evaluation_scope"] == "sampled"
    picked = report["sampled_episodes"]
    assert 1 in picked and 20 in picked
    assert len(picked) < 20
    for i in picked:
        assert f"EZ{i}_" in user
    for i in [x for x in range(1, 21) if x not in picked]:
        assert f"EZ{i}_" not in user


def test_evaluate_report_still_has_required_fields(monkeypatch):
    text = _script(20)
    report, _ = _run_evaluate(monkeypatch, text, _Settings(window=60, ratio=0.6))
    for key in ("strengths", "issues", "suggestions", "production_risks",
                "plot_holes", "overall_score", "summary"):
        assert key in report
    assert 1 <= report["overall_score"] <= 10
