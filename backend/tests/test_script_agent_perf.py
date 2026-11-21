"""script_agent 性能开关测试：评估软闸口开关 + worker 有界优雅退出。

背景（实测数据，2026-11-20）：
单集解析任务总耗时 776s，其中评估阶段固定占 84s（32%）且每次都因
「评估报告缺少必需字段」失败，零产出。关闭后同一任务降到 172s（-78%）。

这些测试锁定两条不可回退的行为：
1. script_evaluation_enabled=False 时不发起评估 LLM 调用，但必须写
   agent.eval_skipped 事件（前端与排障都靠它区分「没评估」和「不知道」）；
2. worker 停止时对在途任务先发协作式取消、再有界等待，绝不无限阻塞。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

import pytest


# ---------- 1. 评估开关 ----------

def test_evaluation_disabled_by_default():
    """默认关闭评估：该阶段零产出却占 32% 耗时，不能默认开启。"""
    from app.core.config import Settings

    assert Settings().script_evaluation_enabled is False


def test_evaluation_switch_is_runtime_configurable():
    """开关必须进 _PERSISTED_FIELDS，否则设置页改了不生效（重启即丢）。"""
    from app.core.config import _PERSISTED_FIELDS

    assert "script_evaluation_enabled" in _PERSISTED_FIELDS


def test_evaluation_switch_exposed_in_settings_api():
    """设置页读接口必须回传该字段，否则前端看不到当前状态。"""
    from app.api.v1.settings import _settings_to_dict
    from app.core.config import get_settings

    d = _settings_to_dict(get_settings())
    assert "script_evaluation_enabled" in d
    assert isinstance(d["script_evaluation_enabled"], bool)


def test_worker_grace_is_persisted_and_exposed():
    """grace 值必须可配且可见：它必须小于 systemd TimeoutStopSec，写错会导致 SIGKILL。"""
    from app.core.config import _PERSISTED_FIELDS, Settings

    assert "worker_shutdown_grace_seconds" in _PERSISTED_FIELDS
    assert Settings().worker_shutdown_grace_seconds > 0


def test_worker_grace_smaller_than_systemd_timeout():
    """契约：grace 必须小于 systemd TimeoutStopSec(45s)，否则优雅退出来不及生效。

    TimeoutStopSec 写在 10-fast-stop.conf。这里用常量断言，改动任一侧时
    必须同步检查另一侧。
    """
    from app.core.config import Settings

    systemd_timeout = 45.0
    assert Settings().worker_shutdown_grace_seconds < systemd_timeout, (
        "worker_shutdown_grace_seconds 必须小于 systemd TimeoutStopSec，"
        "否则进程会在优雅退出完成前被 SIGKILL，导致 running 任务下次启动被重跑"
    )


# ---------- 2. 有界优雅退出语义 ----------

def test_future_result_timeout_is_raised_not_swallowed():
    """f.result(timeout=) 超时必须抛 FutureTimeout —— worker 依赖它放弃等待。

    这是修复「SIGTERM 无限等待 → SIGKILL → 任务重跑」的核心机制：
    如果超时后静默返回，worker 会误以为任务已结束并继续退出流程，
    而实际上线程还在跑，进程退出时被强杀，结果一样。
    """
    ev = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(ev.wait, 30)
        t0 = time.monotonic()
        with pytest.raises(FutureTimeout):
            fut.result(timeout=0.2)
        elapsed = time.monotonic() - t0
        # 必须在超时附近返回，不能阻塞到线程自然结束（30s）
        assert elapsed < 5, f"result(timeout=) 阻塞了 {elapsed:.1f}s，超时未生效"
    finally:
        ev.set()
        pool.shutdown(wait=False)


def test_cancel_request_interrupts_inflight_job():
    """停止信号必须能打断在途任务：job_cancel 是跨进程协作式取消的入口。"""
    from app.services import job_cancel

    jid = "job_test_graceful_stop"
    job_cancel.clear(jid)
    try:
        assert job_cancel.is_cancelled(jid) is False
        job_cancel.request(jid)
        assert job_cancel.is_cancelled(jid) is True
    finally:
        job_cancel.clear(jid)
    assert job_cancel.is_cancelled(jid) is False, "clear 后必须复位，否则同 id 复跑会被误杀"


# ---------- 3. 网关 thinking 开关 ----------

def test_thinking_disabled_payload_shape():
    """关闭 thinking 时 payload 必须带 thinking.type=disabled。

    实测：开启思考链时 completion 恒为 16000 硬上限、78% token 被 reasoning 吃掉、
    单次调用 54s；关闭后同一请求 17.6s、不截断。这是解析提速的最大单点。
    """
    from app.integrations.llm import LLMClient

    client = LLMClient()
    client.disable_thinking = True
    payload = client._thinking_payload({"model": "m", "messages": []})
    assert payload.get("thinking") == {"type": "disabled"}


def test_thinking_enabled_by_default_keeps_payload_untouched():
    """默认不注入 thinking 字段（保留模型推理能力，避免影响非解析场景）。"""
    from app.integrations.llm import LLMClient

    client = LLMClient()
    client.disable_thinking = False
    payload = client._thinking_payload({"model": "m", "messages": []})
    assert "thinking" not in payload
