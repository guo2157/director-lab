"""模型输出能力校验（唯一事实来源）。

## 为什么需要这个模块

2026-11-21 实测事故：项目配了 ``llm_parse_max_tokens=65536``、
``llm_parse_hard_max_tokens=131072``，但实际使用的 ``deepseek-v4.1-flash``
在网关上 ``max_output_tokens`` 只有 **16000**。后果：

- 每次解析首轮都精确撞 16000 被截断（`completion_tokens=16000`），
- 网关还把 ``finish_reason`` 谎报成 ``stop``，日志上看起来「正常结束」，
- ``llm_retry`` 的「截断就翻倍 max_tokens」策略于是**永远空转**——
  翻到 65536 网关照样停在 16000，
- 每个任务白烧 80–90 秒首轮请求，部分任务直接失败。

**配置值与供应商能力脱钩**是根因。换模型只治当下，下个模型还会踩同样的坑，
所以必须在解析前校验。

## 判定口径

``/v1/models`` 返回的 ``max_output_tokens`` 是该模型的**硬上限**。
请求里写的 ``max_tokens`` 超过它时，网关不会报错，而是静默截断——因此
校验必须在客户端做，不能指望服务端拒绝。

## 设计约束

- **绝不阻断任务**：查不到能力（老网关没有该字段 / 网络失败）时放行，
  只留一条 warning 事件。校验是「防呆」不是「门禁」。
- **不额外增加延迟**：结果按 (base_url, model) 缓存，进程内复用。
- **纯函数优先**：``evaluate_capability`` 不碰网络，便于单测。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

__all__ = [
    "ModelCapability",
    "CapabilityVerdict",
    "evaluate_capability",
    "fetch_capability",
    "check_and_record",
    "clear_cache",
]

#: 能力查询结果缓存时长（秒）。模型上下架不频繁，1 小时足够。
_CACHE_TTL = 3600
#: 网络超时。校验是旁路，绝不能因为它拖慢解析。
_TIMEOUT = 8.0

_cache: dict[tuple[str, str], tuple[float, "ModelCapability"]] = {}
_cache_lock = threading.Lock()


@dataclass
class ModelCapability:
    """单个模型的能力声明。缺失字段用 None 表示「网关未声明」。"""

    model: str
    max_output_tokens: int | None = None
    max_input_tokens: int | None = None
    source: str = "models_api"

    @property
    def known(self) -> bool:
        return self.max_output_tokens is not None


@dataclass
class CapabilityVerdict:
    """校验结论。``ok=False`` 时 ``message`` 面向用户，``detail`` 面向排障。"""

    ok: bool
    code: str | None = None
    message: str = ""
    detail: dict = field(default_factory=dict)

    def to_event(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            **self.detail,
        }


def evaluate_capability(
    cap: ModelCapability | None,
    *,
    parse_max_tokens: int,
    hard_max_tokens: int | None = None,
    model: str = "",
) -> CapabilityVerdict:
    """比对配置预算与模型能力（纯函数，不碰网络）。

    ``cap`` 为 None 或 ``max_output_tokens`` 未声明时返回 ok=True 且
    ``code="capability_unknown"``——不能因为查不到就拦任务。
    """
    name = model or (cap.model if cap else "")
    if cap is None or not cap.known:
        return CapabilityVerdict(
            ok=True,
            code="capability_unknown",
            message=f"未能获取模型 {name or '(未指定)'} 的输出上限，跳过预算校验",
            detail={"model": name, "parse_max_tokens": parse_max_tokens},
        )

    limit = int(cap.max_output_tokens or 0)
    over: list[str] = []
    if parse_max_tokens > limit:
        over.append(f"llm_parse_max_tokens={parse_max_tokens}")
    if hard_max_tokens is not None and hard_max_tokens > limit:
        over.append(f"llm_parse_hard_max_tokens={hard_max_tokens}")

    if not over:
        return CapabilityVerdict(
            ok=True,
            code=None,
            message=f"模型 {name} 输出上限 {limit}，配置预算未超限",
            detail={
                "model": name,
                "max_output_tokens": limit,
                "parse_max_tokens": parse_max_tokens,
                "hard_max_tokens": hard_max_tokens,
            },
        )

    return CapabilityVerdict(
        ok=False,
        code="llm_budget_exceeds_model_limit",
        message=(
            f"模型 {name} 的输出上限是 {limit} token，但配置的 "
            f"{'、'.join(over)} 超过该上限。截断重试时放大 max_tokens 不会生效，"
            f"每次解析都会白跑一轮。请把解析预算调到 ≤ {limit}，"
            f"或改用输出上限更大的模型。"
        ),
        detail={
            "model": name,
            "max_output_tokens": limit,
            "parse_max_tokens": parse_max_tokens,
            "hard_max_tokens": hard_max_tokens,
            "over_limit_fields": over,
        },
    )


def fetch_capability(base_url: str, api_key: str, model: str, *,
                     use_cache: bool = True) -> ModelCapability | None:
    """从 OpenAI 兼容 ``/models`` 取指定模型的能力声明。

    任何失败都返回 None（由调用方按「未知」处理），绝不抛异常：
    这是旁路校验，不能因为它让解析失败。
    """
    key = ((base_url or "").rstrip("/"), model or "")
    now = time.time()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and now - hit[0] < _CACHE_TTL:
                return hit[1]

    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(
                f"{key[0]}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("data") or []
    except Exception:  # noqa: BLE001 — 旁路校验，任何异常都降级为「未知」
        return None

    found: ModelCapability | None = None
    for m in items:
        if str(m.get("id") or "") != model:
            continue
        out = m.get("max_output_tokens")
        inp = m.get("max_input_tokens")
        found = ModelCapability(
            model=model,
            max_output_tokens=int(out) if out is not None else None,
            max_input_tokens=int(inp) if inp is not None else None,
        )
        break

    if found is None:
        # 模型不在列表里：视为未知，不缓存（可能是临时下架）
        return None

    if use_cache:
        with _cache_lock:
            _cache[key] = (now, found)
    return found


def check_and_record(settings, *, write_event=None, log=None) -> CapabilityVerdict:
    """解析前调用：查能力 → 比对 → 写事件。

    ``write_event`` 签名为 ``(event_type: str, payload: dict) -> None``，
    不传则只记日志。返回结论供调用方决定是否继续（当前策略：始终继续）。
    """
    model = getattr(settings, "llm_model", "") or ""
    cap = fetch_capability(
        getattr(settings, "llm_base_url", "") or "",
        getattr(settings, "llm_api_key", "") or "",
        model,
    )
    verdict = evaluate_capability(
        cap,
        parse_max_tokens=int(getattr(settings, "llm_parse_max_tokens", 0) or 0),
        hard_max_tokens=int(getattr(settings, "llm_parse_hard_max_tokens", 0) or 0),
        model=model,
    )

    if write_event is not None:
        try:
            write_event("agent.model_capability", verdict.to_event())
        except Exception:  # noqa: BLE001
            pass
    if not verdict.ok and log is not None:
        try:
            log.warning("模型预算超限: %s", verdict.message)
        except Exception:  # noqa: BLE001
            pass
    return verdict


def clear_cache() -> None:
    """测试用：清空能力缓存。"""
    with _cache_lock:
        _cache.clear()
