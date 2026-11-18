"""LLM 重试策略测试：按错误类型分流、token 真正放大、尝试记录完整。

核心验证点（对应需求 §6）：
- 首次空输出、第二次成功 → 成功且 attempt 记录两次
- 连续三次空输出 → 抛出 llm_empty_content
- 截断 → max_tokens 严格递增并真正传入下一次请求
- 普通文本 → 追加 JSON 修复提示，不放大 token
- 限流 → 退避重试
- 每次尝试都记录 attempt / max_tokens / finish_reason / usage / request_id
"""
from __future__ import annotations

import pytest

from app.integrations.llm import LLMError
from app.integrations.llm_response import (
    ERR_EMPTY_CONTENT,
    ERR_NON_JSON_OUTPUT,
    ERR_OUTPUT_TRUNCATED,
    ERR_PROVIDER_ERROR,
)
from app.integrations.llm_retry import (
    AttemptRecord,
    _next_max_tokens,
    call_with_retry,
)


class FakeClient:
    """可编程的假客户端：按脚本返回内容或抛错，并记录每次请求参数。"""

    def __init__(self, script, *, max_tokens=32768):
        self.script = list(script)
        self.max_tokens = max_tokens
        self.calls: list[dict] = []
        self.last_metadata = None
        self._i = 0

    def _next(self, prompt, max_tokens):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        item = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        if isinstance(item, Exception):
            # 模拟 client.last_metadata（错误路径也要能取到 finish_reason/usage）
            self.last_metadata = _Meta(finish_reason="stop", completion_tokens=16000,
                                       reasoning_tokens=11000, prompt_tokens=100)
            raise item
        self.last_metadata = _Meta(finish_reason="stop", completion_tokens=200,
                                   reasoning_tokens=0, prompt_tokens=100,
                                   content_length=len(item or ""))
        return item

    def chat_stream(self, system_prompt, user_prompt, *, json_mode=True,
                    on_delta=None, max_tokens=None):
        return self._next(user_prompt, max_tokens)

    def chat(self, system_prompt, user_prompt, *, json_mode=True, max_tokens=None):
        return self._next(user_prompt, max_tokens)


class _Meta:
    """模拟 LLMClient.last_metadata（含 content_length，供失败记录回落取值）。"""

    def __init__(self, *, finish_reason, completion_tokens, reasoning_tokens,
                 prompt_tokens, content_length=0, reasoning_length=0):
        self.model = "fake-model"
        self.max_tokens = 0
        self.finish_reason = finish_reason
        self.completion_tokens = completion_tokens
        self.reasoning_tokens = reasoning_tokens
        self.prompt_tokens = prompt_tokens
        self.content_length = content_length
        self.reasoning_length = reasoning_length


def _run(client, **kw):
    kw.setdefault("system_prompt", "sys")
    kw.setdefault("user_prompt", "user")
    kw.setdefault("retry_limit", 2)
    return call_with_retry(client, **kw)


# ══════════════════════════════════════════════ 15. 第一次空、第二次成功

def test_first_empty_then_success():
    client = FakeClient([LLMError(ERR_EMPTY_CONTENT, "空"), '{"a": 1}'])
    records: list[AttemptRecord] = []
    out = _run(client, on_attempt=records.append)
    assert out == '{"a": 1}'
    assert len(client.calls) == 2
    assert [r.attempt for r in records] == [1, 2]
    assert records[0].ok is False and records[0].error_code == ERR_EMPTY_CONTENT
    assert records[1].ok is True


# ══════════════════════════════════════════════ 16. 连续三次空输出

def test_three_consecutive_empty_raises():
    client = FakeClient([LLMError(ERR_EMPTY_CONTENT, "空")] * 3)
    with pytest.raises(LLMError) as ei:
        _run(client, retry_limit=2)
    assert ei.value.code == ERR_EMPTY_CONTENT
    # retry_limit=2 → 总共 3 次请求
    assert len(client.calls) == 3


def test_retry_limit_zero_means_single_request():
    client = FakeClient([LLMError(ERR_EMPTY_CONTENT, "空")] * 3)
    with pytest.raises(LLMError):
        _run(client, retry_limit=0)
    assert len(client.calls) == 1


# ══════════════════════════════════════════════ 截断 → token 真正放大

def test_truncated_widens_max_tokens_and_actually_passes_it():
    """旧实现曾在 min() 里把放大值夹回原值，导致原地空转。"""
    client = FakeClient(
        [LLMError(ERR_OUTPUT_TRUNCATED, "截断"), LLMError(ERR_OUTPUT_TRUNCATED, "截断"),
         '{"a": 1}'],
        max_tokens=32768,
    )
    out = _run(client, retry_limit=2, widen_floor=65536, hard_max_tokens=131072)
    assert out == '{"a": 1}'
    budgets = [c["max_tokens"] for c in client.calls]
    assert budgets[0] == 32768
    assert budgets[1] > budgets[0], f"第二次预算必须放大，实际 {budgets}"
    assert budgets[2] > budgets[1], f"第三次预算必须继续放大，实际 {budgets}"
    # 放大后的值必须真正传进请求
    assert budgets[1] == 65536
    assert budgets[2] == 131072


def test_truncated_appends_compact_output_hint():
    client = FakeClient([LLMError(ERR_OUTPUT_TRUNCATED, "截断"), '{"a": 1}'])
    _run(client)
    assert "输出约束" in client.calls[1]["prompt"]
    assert "截断" in client.calls[1]["prompt"]


def test_max_tokens_never_exceeds_hard_cap():
    assert _next_max_tokens(131072, floor=65536, hard=131072) == 131072
    assert _next_max_tokens(100000, floor=65536, hard=131072) <= 131072


def test_next_max_tokens_is_strictly_increasing_even_at_cap():
    """即使已到上限，也要返回一个更大的值（否则重试等于空转）。"""
    v = _next_max_tokens(131072, floor=65536, hard=262144)
    assert v > 131072


# ══════════════════════════════════════════════ 非 JSON → 修复提示，不放大 token

def test_non_json_appends_json_only_hint_without_widening():
    client = FakeClient([LLMError(ERR_NON_JSON_OUTPUT, "非JSON"), '{"a": 1}'])
    _run(client, retry_limit=2)
    assert client.calls[0]["max_tokens"] == client.calls[1]["max_tokens"], \
        "格式错误不应放大 token"
    assert "只输出一个 JSON 对象" in client.calls[1]["prompt"]


# ══════════════════════════════════════════════ 限流 → 退避

def test_rate_limit_is_retried_with_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("app.integrations.llm_retry.time.sleep", lambda s: slept.append(s))
    client = FakeClient([LLMError(ERR_PROVIDER_ERROR, "HTTP 429 rate limit"), '{"a": 1}'])
    out = _run(client, retry_limit=2)
    assert out == '{"a": 1}'
    assert slept and slept[0] >= 1.0


def test_unknown_error_is_not_retried(monkeypatch):
    monkeypatch.setattr("app.integrations.llm_retry.time.sleep", lambda s: None)
    client = FakeClient([LLMError("llm_unconfigured", "未配置")] * 3)
    with pytest.raises(LLMError) as ei:
        _run(client, retry_limit=2)
    assert ei.value.code == "llm_unconfigured"
    assert len(client.calls) == 1, "未识别错误不应盲目重试"


# ══════════════════════════════════════════════ 尝试记录完整性

def test_attempt_record_contains_all_required_fields():
    client = FakeClient([LLMError(ERR_OUTPUT_TRUNCATED, "截断"), '{"a": 1}'])
    records: list[AttemptRecord] = []
    _run(client, on_attempt=records.append)
    d = records[0].to_dict()
    for key in ("attempt", "max_tokens", "error_code", "finish_reason",
                "content_length", "reasoning_length", "completion_tokens",
                "reasoning_tokens", "request_id", "duration_ms", "ok"):
        assert key in d, f"缺少字段 {key}"
    assert d["attempt"] == 1
    assert d["max_tokens"] == 32768


# ══════════════════════════════════════════════ validate 回调在循环内

def test_validate_callback_failure_triggers_retry():
    """正文拿到了但 JSON 不合法 → 必须重试，不能直接失败。"""
    client = FakeClient(["这不是JSON", '{"a": 1}'])

    def _validate(text):
        try:
            return __import__("json").loads(text)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(ERR_NON_JSON_OUTPUT, f"非 JSON: {exc}") from exc

    out = _run(client, retry_limit=2, validate=_validate)
    assert out == {"a": 1}
    assert len(client.calls) == 2


def test_validate_callback_returns_parsed_value():
    client = FakeClient(['{"a": 1}'])
    out = _run(client, validate=lambda t: __import__("json").loads(t))
    assert out == {"a": 1}


# ══════════════════════════════════════════════ 失败记录的元数据不能丢

def test_failed_attempt_records_real_content_length_from_exception():
    """失败记录必须反映失败那次响应的真实长度，不能硬编码 0。

    真实故障：attempt=1 思考链吃光 16000 token、正文 0 字符，
    如果 content_length 一律记 0，「空正文」和「正文拿到了但 JSON 坏」
    在事件里完全无法区分，排查只能靠猜。
    """
    from app.integrations.llm_response import LLMResponseMeta

    # attempt 1：思考链吃光预算，正文 0 字符
    empty_meta = LLMResponseMeta(
        content="", reasoning_content="思考" * 8000, finish_reason="stop",
        usage={"completion_tokens": 16000, "reasoning_tokens": 16000,
               "prompt_tokens": 100},
    )
    exc = LLMError(ERR_EMPTY_CONTENT, "空正文", meta=empty_meta)
    client = FakeClient([exc, '{"a": 1}'])
    records: list[AttemptRecord] = []
    out = _run(client, on_attempt=records.append)
    assert out == '{"a": 1}'
    assert records[0].content_length == 0        # 空正文如实记录 0
    assert records[0].reasoning_length == 16000  # 但思考链长度必须保留
    assert records[0].usage["completion_tokens"] == 16000
    assert records[0].finish_reason == "stop"


def test_failed_attempt_records_nonzero_content_when_body_present():
    """正文拿到了但 JSON 不合法：content_length 必须非 0。"""
    from app.integrations.llm_response import LLMResponseMeta

    meta = LLMResponseMeta(
        content="正文3038字符" * 300, finish_reason="stop",
        usage={"completion_tokens": 12445, "reasoning_tokens": 10812,
               "prompt_tokens": 100},
    )
    exc = LLMError(ERR_OUTPUT_TRUNCATED, "截断", meta=meta)
    client = FakeClient([exc, '{"a": 1}'])
    records: list[AttemptRecord] = []
    _run(client, on_attempt=records.append)
    assert records[0].content_length == len("正文3038字符" * 300)
    assert records[0].reasoning_length == 0
    assert records[0].usage["reasoning_tokens"] == 10812


def test_validate_error_falls_back_to_client_metadata():
    """validate 回调抛的异常没有 meta，应回落到 client.last_metadata。"""
    client = FakeClient(["正文3038字符", '{"a": 1}'])
    records: list[AttemptRecord] = []
    state = {"n": 0}

    def _validate(text):
        state["n"] += 1
        if state["n"] == 1:
            raise LLMError(ERR_NON_JSON_OUTPUT, "schema 不符")
        return __import__("json").loads(text)

    out = _run(client, retry_limit=2, on_attempt=records.append, validate=_validate)
    assert out == {"a": 1}
    assert records[0].content_length == 8   # "正文3038字符" 8 字符，不能是 0
    assert records[0].usage["completion_tokens"] == 200
