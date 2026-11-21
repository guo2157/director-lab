"""模型输出能力校验的单元测试（纯函数 + 缓存 + 降级）。

覆盖 2026-11-21 事故场景：配置 llm_parse_max_tokens=65536 但模型上限 16000，
网关静默截断 + finish_reason 谎报 stop，导致翻倍重试永远空转。
"""
from __future__ import annotations

import pytest

from app.integrations import model_capability as mc


@pytest.fixture(autouse=True)
def _clear():
    mc.clear_cache()
    yield
    mc.clear_cache()


# ---------------------------------------------------------------- 纯函数

def test_over_limit_is_flagged():
    """配置 65536 > 模型 16000 → 必须报超限（本次事故的配置）。"""
    cap = mc.ModelCapability(model="deepseek-v4.1-flash", max_output_tokens=16000)
    v = mc.evaluate_capability(
        cap, parse_max_tokens=65536, hard_max_tokens=131072,
        model="deepseek-v4.1-flash",
    )
    assert v.ok is False
    assert v.code == "llm_budget_exceeds_model_limit"
    # 两个字段都要被点出来，否则用户只改一个还会踩
    assert "llm_parse_max_tokens" in v.message
    assert "llm_parse_hard_max_tokens" in v.message
    assert v.detail["max_output_tokens"] == 16000
    assert len(v.detail["over_limit_fields"]) == 2


def test_within_limit_passes():
    """配置 64000 ≤ 模型 64000 → 通过（当前切换后的配置）。"""
    cap = mc.ModelCapability(model="deepseek-v4.1-flash-max", max_output_tokens=64000)
    v = mc.evaluate_capability(
        cap, parse_max_tokens=64000, hard_max_tokens=64000,
        model="deepseek-v4.1-flash-max",
    )
    assert v.ok is True
    assert v.code is None


def test_hard_limit_only_over():
    """只 hard 超限也要报出来：hard 是重试放大后的落点。"""
    cap = mc.ModelCapability(model="m", max_output_tokens=32000)
    v = mc.evaluate_capability(
        cap, parse_max_tokens=32000, hard_max_tokens=131072, model="m",
    )
    assert v.ok is False
    assert len(v.detail["over_limit_fields"]) == 1
    assert "llm_parse_hard_max_tokens" in v.detail["over_limit_fields"][0]
    assert "llm_parse_max_tokens" not in v.detail["over_limit_fields"][0]


def test_unknown_capability_does_not_block():
    """查不到能力时必须放行（code=capability_unknown），不能误杀任务。"""
    v = mc.evaluate_capability(None, parse_max_tokens=999999, model="mystery")
    assert v.ok is True
    assert v.code == "capability_unknown"
    assert v.detail["model"] == "mystery"


def test_declared_none_output_limit_does_not_block():
    """模型在列表里但没声明 max_output_tokens → 视为未知，放行。"""
    cap = mc.ModelCapability(model="m", max_output_tokens=None)
    v = mc.evaluate_capability(cap, parse_max_tokens=999999, model="m")
    assert v.ok is True
    assert v.code == "capability_unknown"


def test_to_event_shape():
    """事件 payload 必须是扁平可 JSON 化的 dict（落库要求）。"""
    cap = mc.ModelCapability(model="m", max_output_tokens=16000)
    v = mc.evaluate_capability(cap, parse_max_tokens=65536, model="m")
    ev = v.to_event()
    assert ev["ok"] is False
    assert ev["code"] == "llm_budget_exceeds_model_limit"
    assert isinstance(ev["message"], str)
    assert ev["max_output_tokens"] == 16000


# ---------------------------------------------------------------- 网络层降级

def test_fetch_returns_none_on_network_error(monkeypatch):
    """网络失败必须返回 None，绝不抛异常（旁路校验不能拖垮解析）。"""
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx.Client, "get", boom)
    assert mc.fetch_capability("http://127.0.0.1:1/v1", "k", "m") is None


def test_fetch_returns_none_when_model_absent(monkeypatch):
    """模型不在 /models 列表里 → None（视为未知）。"""
    import httpx

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "other-model", "max_output_tokens": 8000}]}

    monkeypatch.setattr(httpx.Client, "get", lambda self, *a, **kw: _Resp())
    assert mc.fetch_capability("http://x/v1", "k", "wanted-model") is None


def test_fetch_parses_and_caches(monkeypatch):
    """正常路径：解析出 max_output_tokens，且第二次命中缓存（不再发请求）。"""
    import httpx

    calls = {"n": 0}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"id": "deepseek-v4.1-flash", "max_output_tokens": 16000,
                 "max_input_tokens": 116000},
                {"id": "deepseek-v4.1-flash-max", "max_output_tokens": 64000,
                 "max_input_tokens": 1000000},
            ]}

    def fake_get(self, *a, **kw):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    cap = mc.fetch_capability("http://x/v1", "k", "deepseek-v4.1-flash")
    assert cap is not None
    assert cap.max_output_tokens == 16000
    assert cap.max_input_tokens == 116000

    cap2 = mc.fetch_capability("http://x/v1", "k", "deepseek-v4.1-flash")
    assert cap2.max_output_tokens == 16000
    assert calls["n"] == 1, "第二次应命中缓存"

    # 不同模型不共用缓存
    cap3 = mc.fetch_capability("http://x/v1", "k", "deepseek-v4.1-flash-max")
    assert cap3.max_output_tokens == 64000
    assert calls["n"] == 2


# ---------------------------------------------------------------- check_and_record

class _S:
    llm_model = "deepseek-v4.1-flash"
    llm_base_url = "http://x/v1"
    llm_api_key = "k"
    llm_parse_max_tokens = 65536
    llm_parse_hard_max_tokens = 131072


def test_check_and_record_writes_event(monkeypatch):
    """超限时必须写出 agent.model_capability 事件，供前端提示。"""
    monkeypatch.setattr(
        mc, "fetch_capability",
        lambda *a, **kw: mc.ModelCapability("deepseek-v4.1-flash", max_output_tokens=16000),
    )
    events = []
    v = mc.check_and_record(_S(), write_event=lambda et, p: events.append((et, p)))
    assert v.ok is False
    assert len(events) == 1
    et, payload = events[0]
    assert et == "agent.model_capability"
    assert payload["ok"] is False
    assert payload["max_output_tokens"] == 16000


def test_check_and_record_survives_event_failure(monkeypatch):
    """写事件失败不能让校验抛异常（解析还要继续跑）。"""
    monkeypatch.setattr(
        mc, "fetch_capability",
        lambda *a, **kw: mc.ModelCapability("m", max_output_tokens=16000),
    )

    def bad_event(et, p):
        raise RuntimeError("db down")

    v = mc.check_and_record(_S(), write_event=bad_event)
    assert v.ok is False  # 结论仍然返回


def test_check_and_record_unknown_is_ok(monkeypatch):
    """查不到能力 → ok=True，任务照常继续。"""
    monkeypatch.setattr(mc, "fetch_capability", lambda *a, **kw: None)
    events = []
    v = mc.check_and_record(_S(), write_event=lambda et, p: events.append((et, p)))
    assert v.ok is True
    assert v.code == "capability_unknown"
    assert events[0][1]["code"] == "capability_unknown"
