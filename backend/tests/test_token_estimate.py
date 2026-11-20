"""阶段 2 测试：输入 token 估算、预算预检、输入超限错误码与不重试行为。

覆盖：
- estimate_tokens：空串、纯中文、纯英文、中英混合、单调性
- input_budget：窗口为 0 时返回 0（跳过预检）、比例计算
- precheck_input_budget：未配置窗口放行、整本超预算、单集超预算、
  allow_segmented 开关、错误文案含集号与预算
- validate_llm_response：供应商响应体含 maximum context length →
  llm_input_too_long 且 retryable=False；普通 401 仍是 provider_error
- call_with_retry：llm_input_too_long 只发 1 次请求，不退避不放大 token
"""
from __future__ import annotations

import pytest

from app.integrations.llm import LLMError
from app.integrations.llm_response import (
    ERR_INPUT_TOO_LONG,
    ERR_PROVIDER_ERROR,
    LLMResponseMeta,
    is_input_too_long_error,
    validate_llm_response,
)
from app.integrations.llm_retry import call_with_retry
from app.integrations.token_estimate import (
    estimate_input_tokens,
    estimate_tokens,
    input_budget,
    precheck_input_budget,
)


class _Settings:
    """最小配置桩：只有预检相关字段，其余走 getattr 默认值。"""

    def __init__(self, *, window=128000, ratio=0.6, cjk=1.2, other=0.3):
        self.llm_context_window_tokens = window
        self.llm_input_budget_ratio = ratio
        self.llm_cjk_token_factor = cjk
        self.llm_other_token_factor = other


# ============================================== estimate_tokens
def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_pure_cjk():
    # 10 个 CJK 字符 × 1.2 = 12
    assert estimate_tokens("一二三四五六七八九十") == 12


def test_estimate_tokens_pure_ascii():
    # 10 个 ASCII 字符 × 0.3 = 3
    assert estimate_tokens("abcdefghij") == 3


def test_estimate_tokens_mixed_is_between_extremes():
    text = "你好世界hello"
    mixed = estimate_tokens(text)
    assert estimate_tokens("你好世界") < mixed
    # 混合结果应等于两类之和（向上取整）
    raw = 4 * 1.2 + 5 * 0.3
    assert mixed == int(raw) + (1 if raw > int(raw) else 0)


def test_estimate_tokens_monotonic():
    """长度单调：文本越长，估算 token 不减。"""
    base = "林昭：我回来了。\n陈默：三年了。\n"
    prev = 0
    for n in range(1, 20):
        cur = estimate_tokens(base * n)
        assert cur >= prev
        prev = cur


def test_estimate_tokens_respects_factors():
    text = "你好世界"
    loose = estimate_tokens(text, cjk_factor=0.5)
    tight = estimate_tokens(text, cjk_factor=2.0)
    assert loose < tight


def test_estimate_input_tokens_reads_settings():
    text = "你好世界"
    s = _Settings(cjk=1.0)
    assert estimate_input_tokens(text, settings=s) == 4
    assert estimate_input_tokens(text, settings=None) == estimate_tokens(text)


# ============================================== input_budget
def test_input_budget_zero_window_disables_precheck():
    assert input_budget(_Settings(window=0)) == 0
    assert input_budget(_Settings(window=-1)) == 0


def test_input_budget_ratio():
    assert input_budget(_Settings(window=100000, ratio=0.6)) == 60000
    assert input_budget(_Settings(window=100000, ratio=0.0)) == 0


# ============================================== precheck_input_budget
SCRIPT_3 = (
    "第1集 归途\n" + "林昭：第一集的台词内容。" * 20 + "\n\n"
    "第2集 旧信\n" + "陈默：第二集的台词内容。" * 20 + "\n\n"
    "第3集 落幕\n" + "林昭：第三集的台词内容。" * 20
)


def test_precheck_pass_when_window_unset():
    """窗口为 0（未知）时跳过预检，任何长度都放行。"""
    s = _Settings(window=0)
    res = precheck_input_budget("第1集\n" + "超长内容" * 100000, settings=s)
    assert res["ok"] is True
    assert res["budget"] == 0


def test_precheck_pass_when_within_budget():
    s = _Settings(window=1000000, ratio=0.9)
    res = precheck_input_budget(SCRIPT_3, settings=s)
    assert res["ok"] is True
    assert res["outline_mode"] == "full"
    assert res["code"] is None
    assert res["estimated_tokens"] > 0


def test_precheck_fails_when_single_episode_exceeds_budget():
    """单集正文放不下 → 必失败，即使整本本来也不大。"""
    s = _Settings(window=100, ratio=0.5)  # 预算 50 token
    res = precheck_input_budget(SCRIPT_3, settings=s)
    assert res["ok"] is False
    assert res["code"] == ERR_INPUT_TOO_LONG
    assert res["episode"] is not None
    assert "第" in res["message"]
    assert "预算" in res["message"]


def test_precheck_whole_script_exceeds_without_segmented():
    """整本超预算且不允许分段 → 失败，提示拆分或调大窗口。"""
    eps = "\n".join(f"第{i}集\n" + "林昭：短台词。" * 8 for i in range(1, 31))
    s = _Settings(window=100000, ratio=0.001)  # 预算 100 token
    one = "第1集\n" + "林昭：短台词。" * 8
    assert estimate_tokens(one) < 100  # 单集放得下
    res = precheck_input_budget(eps, settings=s)
    assert res["ok"] is False
    assert res["code"] == ERR_INPUT_TOO_LONG
    assert res["episode"] is None
    assert "llm_context_window_tokens" in res["message"]


def test_precheck_whole_script_exceeds_with_segmented_allowed():
    """允许分段时整本超预算只标记 outline_mode=segmented，不失败。"""
    eps = "\n".join(f"第{i}集\n" + "林昭：短台词。" * 8 for i in range(1, 31))
    s = _Settings(window=100000, ratio=0.001)
    res = precheck_input_budget(eps, settings=s, allow_segmented=True)
    assert res["ok"] is True
    assert res["outline_mode"] == "segmented"
    assert res["code"] is None


def test_precheck_without_markers_uses_whole_text():
    """无集标记时单集=整本，超预算即失败并提示无标记。"""
    s = _Settings(window=100, ratio=0.5)
    res = precheck_input_budget("这是一段没有分集标记的长剧本。" * 200, settings=s)
    assert res["ok"] is False
    assert res["code"] == ERR_INPUT_TOO_LONG
    assert res["episode"] is None
    assert "无集标记" in res["message"]


# ============================================== 特征词映射
@pytest.mark.parametrize(
    "msg",
    [
        "HTTP 400: This model's maximum context length is 65536 tokens",
        "HTTP 400: context_length_exceeded",
        "HTTP 413: Request too large for model",
        "HTTP 400: prompt is too long",
        "HTTP 400: 输入内容过长，超过最大上下文长度",
    ],
)
def test_is_input_too_long_error_positive(msg):
    assert is_input_too_long_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        None, "", "HTTP 401: invalid api key",
        "HTTP 429: rate limit exceeded",
        "HTTP 500: internal error",
    ],
)
def test_is_input_too_long_error_negative(msg):
    assert is_input_too_long_error(msg) is False


def test_validate_maps_context_length_to_input_too_long():
    meta = LLMResponseMeta(
        content=None,
        provider_error="HTTP 400: maximum context length is 65536 tokens",
        model="fake",
        max_tokens=32768,
    )
    res = validate_llm_response(meta)
    assert res.valid is False
    assert res.error_code == ERR_INPUT_TOO_LONG
    assert res.retryable is False


def test_validate_plain_provider_error_still_retryable():
    meta = LLMResponseMeta(
        content=None, provider_error="HTTP 500: internal error",
        model="fake", max_tokens=32768,
    )
    res = validate_llm_response(meta)
    assert res.error_code == ERR_PROVIDER_ERROR
    assert res.retryable is True


def test_truncation_is_not_mistaken_for_input_too_long():
    """输出截断（finish_reason=length）绝不能被判成输入超限：处置方式相反。"""
    meta = LLMResponseMeta(
        content='{"scenes": [', finish_reason="length",
        model="fake", max_tokens=32768,
    )
    res = validate_llm_response(meta)
    assert res.error_code != ERR_INPUT_TOO_LONG


# ============================================== call_with_retry 不重试
class _Meta:
    def __init__(self):
        self.model = "fake-model"
        self.max_tokens = 0
        self.finish_reason = None
        self.completion_tokens = None
        self.reasoning_tokens = None
        self.prompt_tokens = 100
        self.content_length = 0
        self.reasoning_length = 0


class _InputTooLongClient:
    """替身：每次都抛输入超限，并记录被调用次数。"""

    def __init__(self):
        self.max_tokens = 32768
        self.last_metadata = None
        self.calls: list[dict] = []

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None):
        self.calls.append({"prompt": user_prompt, "max_tokens": max_tokens})
        self.last_metadata = _Meta()
        raise LLMError(
            ERR_INPUT_TOO_LONG,
            "输入超过模型上下文窗口: HTTP 400 maximum context length",
        )


def test_call_with_retry_does_not_retry_input_too_long():
    """输入超限只发 1 次请求：不重试、不退避、不放大 token。"""
    client = _InputTooLongClient()
    with pytest.raises(LLMError) as ei:
        call_with_retry(
            client, system_prompt="sys", user_prompt="user",
            retry_limit=2,  # 明确允许 2 次重试，仍应只发 1 次
        )
    assert ei.value.code == ERR_INPUT_TOO_LONG
    assert len(client.calls) == 1
    # 提示词未被追加任何放大/修复后缀
    assert client.calls[0]["prompt"] == "user"


def test_call_with_retry_still_retries_provider_error():
    """对照组：普通供应商错误仍然重试，确认上面的断言不是全都不重试。"""

    class _ProviderErrClient:
        def __init__(self):
            self.max_tokens = 32768
            self.last_metadata = None
            self.calls = 0

        def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                        on_delta=None, max_tokens=None):
            self.calls += 1
            self.last_metadata = _Meta()
            raise LLMError(ERR_PROVIDER_ERROR, "HTTP 500: internal error")

    client = _ProviderErrClient()
    with pytest.raises(LLMError):
        call_with_retry(
            client, system_prompt="sys", user_prompt="user", retry_limit=1,
        )
    assert client.calls == 2  # 首次 + 1 次重试
