"""协作式取消的回归测试。

背景（真实故障）：``POST /api/jobs/{id}/cancel`` 原先只把数据库 status 改成
``cancelled``，但 script_agent 的执行线程此刻阻塞在 httpx socket read 上
（单次 LLM 调用实测 24 分钟）。线程感知不到数据库变化，会跑完全程并把 status
覆写回 ``succeeded``，半成品结果照样落库 —— 用户点了「停止解析」，解析继续
烧 token，数据还被改写。

这组测试锁住三件事：
1. 取消信号能真正打断 ``chat_stream`` 的流循环（不必等模型把话说完）；
2. 取消错误码**不进入重试**（否则点停止会再发一轮请求）；
3. ``call_with_retry`` 把 should_cancel 透传到 chat_stream。
"""
from __future__ import annotations

import pytest

from app.integrations.llm import LLMError
from app.integrations.llm_retry import call_with_retry
from app.services import job_cancel


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空登记表，避免跨用例污染。"""
    for jid in ("job_test_cancel", "job_test_normal"):
        job_cancel.clear(jid)
    yield
    for jid in ("job_test_cancel", "job_test_normal"):
        job_cancel.clear(jid)


def test_request_and_query_cancel_signal():
    assert job_cancel.is_cancelled("job_test_cancel") is False
    job_cancel.request("job_test_cancel")
    assert job_cancel.is_cancelled("job_test_cancel") is True
    job_cancel.clear("job_test_cancel")
    assert job_cancel.is_cancelled("job_test_cancel") is False


def test_request_is_idempotent_and_ignores_empty():
    job_cancel.request("job_test_cancel")
    job_cancel.request("job_test_cancel")
    assert job_cancel.is_cancelled("job_test_cancel") is True
    # 空 id 不登记，避免集合里出现无意义键
    job_cancel.request("")
    assert job_cancel.is_cancelled("") is False


def test_cancelled_error_is_not_retried():
    """取消必须立刻冒泡，不能进重试循环。

    旧行为下 ``llm_cancelled`` 未在禁止重试列表中，会被当成未知错误码；
    即便不重试，也可能被 ``_WIDEN_CODES`` / 退避逻辑捕获再跑一轮。
    这里用「只允许调用一次」的替身锁住语义。
    """
    calls = {"n": 0}

    class _Client:
        max_tokens = 1024

        def chat_stream(self, *a, **kw):
            calls["n"] += 1
            raise LLMError("llm_cancelled", "解析已取消")

    with pytest.raises(LLMError) as ei:
        call_with_retry(
            _Client(),
            system_prompt="s",
            user_prompt="u",
            stream=True,
            retry_limit=3,
            should_cancel=lambda: True,
        )
    assert ei.value.code == "llm_cancelled"
    assert calls["n"] == 1, f"取消被重试了 {calls['n']} 次，应只调用一次"


def test_should_cancel_is_passed_through_to_chat_stream():
    """call_with_retry 必须把 should_cancel 透传给 chat_stream。

    不透传的话，chat_stream 内部没有检查点，取消形同虚设 ——
    这正是修复前「点了停止但跑满 24 分钟」的根因。
    """
    seen: dict = {}

    class _Client:
        max_tokens = 1024

        def chat_stream(self, *a, should_cancel=None, **kw):
            seen["should_cancel"] = should_cancel
            return '{"ok": true}'

    sentinel = lambda: False  # noqa: E731
    out = call_with_retry(
        _Client(),
        system_prompt="s",
        user_prompt="u",
        stream=True,
        should_cancel=sentinel,
        validate=lambda text: {"ok": True},
    )
    assert out == {"ok": True}
    assert seen["should_cancel"] is sentinel


def test_cancel_aborts_mid_stream_without_waiting_for_model(monkeypatch):
    """取消要在流中途生效，而不是等模型输出完。

    真实故障场景：模型连续吐 24 分钟，用户点停止后必须立刻退出。
    这里让 should_cancel 在第 3 次检查后变 True，断言只消费了少量 SSE 行。
    """
    from app.integrations import llm as llm_mod

    lines_consumed = {"n": 0}

    class _Resp:
        status_code = 200
        headers: dict = {}

        def iter_lines(self):
            # 模拟无限吐字：真实场景下这里会持续 24 分钟
            for i in range(1000):
                lines_consumed["n"] += 1
                yield 'data: {"choices":[{"delta":{"content":"x"}}]}'
            yield "data: [DONE]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeHTTPClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(llm_mod.httpx, "Client", _FakeHTTPClient)

    client = llm_mod.LLMClient.__new__(llm_mod.LLMClient)  # 跳过 __init__ 的配置校验
    client.base_url = "http://127.0.0.1:1"
    client.api_key = "k"
    client.model = "m"
    client.max_tokens = 100
    client.timeout = 5.0
    # _thinking_payload 会读这两个属性；__new__ 绕过了 __init__，需手工补齐
    client.disable_thinking = False
    client.last_metadata = None

    state = {"n": 0}

    def _should_cancel() -> bool:
        state["n"] += 1
        return state["n"] > 3

    with pytest.raises(LLMError) as ei:
        client.chat_stream("s", "u", should_cancel=_should_cancel)

    assert ei.value.code == "llm_cancelled"
    # 未消费完全部 1000 行就退出
    assert lines_consumed["n"] < 50, (
        f"取消后仍消费了 {lines_consumed['n']} 行，流内检查点未生效"
    )
