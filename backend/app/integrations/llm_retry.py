"""LLM 解析重试策略（唯一事实来源）。

把「重试」从散落在各调用点的 if/else 收敛成一处，按错误码决定**怎么重试**：

| 错误码 | 重试方式 | 理由 |
|---|---|---|
| llm_output_truncated | 放大 max_tokens + 紧凑输出指令 | 预算不够，需要更多 token |
| llm_empty_content | 先原样重试一次，再放大 | 思考吃光预算 vs 偶发空转，两种都可能 |
| llm_stream_interrupted | 退避后原样重试 | 连接问题，加 token 无意义 |
| llm_provider_error / 限流 | 指数退避 | 供应商侧问题 |
| llm_non_json_output | 追加「只输出 JSON」提示 | 格式漂移，加 token 无用 |
| llm_invalid_output | 追加格式修复提示 | 语法错误 |
| llm_schema_mismatch | 追加 Schema 约束提示 | 结构不符 |
| llm_input_too_long | **不重试，立即抛出** | 输入已超上下文窗口，原样重试必然再失败 |

纪律：每次重试都记录 attempt / max_tokens / finish_reason / content_length /
reasoning_length / usage / request_id / error_code，且**放大后的 max_tokens 必须
真正传进下一次请求**（旧实现曾在 min() 里被夹回原值，导致原地重试）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.integrations.llm import LLMClient, LLMError
from app.integrations.llm_response import (
    ERR_EMPTY_CONTENT,
    ERR_INPUT_TOO_LONG,
    ERR_INVALID_OUTPUT,
    ERR_NON_JSON_OUTPUT,
    ERR_OUTPUT_TRUNCATED,
    ERR_PROVIDER_ERROR,
    ERR_SCHEMA_MISMATCH,
    ERR_STREAM_INTERRUPTED,
    LLMResponseMeta,
)

#: 需要放大 token 的错误码
_WIDEN_CODES = {ERR_OUTPUT_TRUNCATED}
#: 需要追加格式修复提示的错误码
_REPAIR_PROMPT_CODES = {ERR_NON_JSON_OUTPUT, ERR_INVALID_OUTPUT, ERR_SCHEMA_MISMATCH}
#: 需要退避的错误码（供应商侧 / 连接侧）
_BACKOFF_CODES = {ERR_PROVIDER_ERROR, ERR_STREAM_INTERRUPTED, "llm_timeout", "llm_unreachable"}
#: 供应商限流特征
_RATE_LIMIT_HINTS = ("429", "rate limit", "too many requests", "quota")

_TRUNCATE_HINT = (
    "\n\n【输出约束】上一次输出在中途被截断。请重新输出完整 JSON："
    "紧凑排版（不使用换行缩进）、省略无实际信息的空值字段，确保 JSON 完整闭合。"
)
_JSON_ONLY_HINT = (
    "\n\n【输出约束】上一次输出不是合法 JSON。本次必须只输出一个 JSON 对象，"
    "不要输出任何解释、前言、Markdown 代码块标记或结尾说明。"
)
_SCHEMA_HINT = (
    "\n\n【输出约束】上一次输出的 JSON 结构不符合要求。请严格按系统提示中的字段定义输出，"
    "顶层必须包含 project_analysis / characters / props / scenes / episodes。"
)
_EMPTY_HINT = (
    "\n\n【输出约束】上一次没有返回任何正文内容。请直接输出最终 JSON 结果，"
    "不要在思考过程中结束；若任务过长，优先保证 JSON 结构完整并精简描述字段。"
)


@dataclass
class AttemptRecord:
    """单次尝试的完整记录（用于事件落库与问题复盘）。"""

    attempt: int
    max_tokens: int
    error_code: str | None = None
    error_message: str | None = None
    finish_reason: str | None = None
    content_length: int = 0
    reasoning_length: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    duration_ms: int = 0
    ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "max_tokens": self.max_tokens,
            "error_code": self.error_code,
            "finish_reason": self.finish_reason,
            "content_length": self.content_length,
            "reasoning_length": self.reasoning_length,
            "completion_tokens": self.usage.get("completion_tokens"),
            "reasoning_tokens": self.usage.get("reasoning_tokens"),
            "prompt_tokens": self.usage.get("prompt_tokens"),
            "request_id": self.request_id,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
        }


def _is_rate_limited(message: str) -> bool:
    low = (message or "").lower()
    return any(h in low for h in _RATE_LIMIT_HINTS)


def _next_max_tokens(current: int, *, floor: int, hard: int) -> int:
    """放大 token 预算：至少翻倍到 floor，且不超过 hard 上限。

    旧实现在此写过 ``min(max(cur*2, 65536), 131072)`` 这类夹取，
    在 cur 已经等于上限时会把值夹回原地，重试变成空转——这里显式保证严格递增。
    """
    target = max(current * 2, floor)
    if target <= current:
        target = current + max(4096, current // 4)
    return min(target, hard)


def call_with_retry(
    client: LLMClient,
    *,
    system_prompt: str,
    user_prompt: str,
    json_mode: bool = True,
    stream: bool = True,
    on_delta: Callable[[str], None] | None = None,
    retry_limit: int | None = None,
    initial_max_tokens: int | None = None,
    widen_floor: int | None = None,
    hard_max_tokens: int | None = None,
    on_attempt: Callable[[AttemptRecord], None] | None = None,
    validate: Callable[[str], Any] | None = None,
    use_chat_json: bool = False,
) -> Any:
    """按错误类型重试地调用 LLM，返回**已通过完整性校验**的结果。

    ``retry_limit`` 语义：首次请求之后最多额外重试 N 次（默认取
    ``llm_parse_retry_limit``，即总请求数 = N + 1）。

    ``validate``：在重试循环**内部**对正文做业务/格式校验（如 JSON 解析），
    失败时抛出 LLMError 即触发下一轮重试。这一步不能放到循环外，否则
    「正文拿到了但 JSON 不合法」就不会重试——旧实现正是如此。
    不传时直接返回正文。

    ``use_chat_json``：走 ``client.chat_json``（非流式 + 内置 JSON 解析）而不是
    ``client.chat``。此时返回值已是 dict，``validate`` 不再生效。
    保留该模式是为了兼容既有调用方与测试替身（它们只实现 chat_json）。

    抛出最后一次的 LLMError（错误码保留，供上层落库）。
    """
    from app.core.config import get_settings

    settings = get_settings()
    if retry_limit is None:
        retry_limit = max(0, int(getattr(settings, "llm_parse_retry_limit", 2) or 0))
    if initial_max_tokens is None:
        # 用 getattr 兜底：测试桩/替身客户端不一定实现 max_tokens
        initial_max_tokens = int(getattr(client, "max_tokens", 0) or 0)
        if initial_max_tokens <= 0:
            initial_max_tokens = int(getattr(settings, "llm_max_tokens", 32768) or 32768)
    if widen_floor is None:
        widen_floor = int(getattr(settings, "llm_parse_max_tokens", 65536) or 65536)
    if hard_max_tokens is None:
        hard_max_tokens = int(getattr(settings, "llm_parse_hard_max_tokens", 131072) or 131072)

    current_max_tokens = int(initial_max_tokens)
    prompt = user_prompt
    last_error: LLMError | None = None
    empty_seen = False

    for attempt in range(retry_limit + 1):
        started = time.monotonic()
        try:
            if use_chat_json:
                # chat_json 内部已做 JSON 解析，返回 dict。
                # 测试替身可能不接受 max_tokens，做一次兼容降级。
                try:
                    value = client.chat_json(system_prompt, prompt,
                                             max_tokens=current_max_tokens)
                except TypeError:
                    value = client.chat_json(system_prompt, prompt)
                content = ""
            elif stream:
                content = client.chat_stream(
                    system_prompt, prompt, json_mode=json_mode,
                    on_delta=on_delta, max_tokens=current_max_tokens,
                )
                value = validate(content) if validate is not None else content
            else:
                content = client.chat(
                    system_prompt, prompt, json_mode=json_mode,
                    max_tokens=current_max_tokens,
                )
                # 格式/业务校验也在重试范围内
                value = validate(content) if validate is not None else content
            meta = getattr(client, "last_metadata", None)
            if on_attempt is not None:
                on_attempt(AttemptRecord(
                    attempt=attempt + 1, max_tokens=current_max_tokens, ok=True,
                    finish_reason=getattr(meta, "finish_reason", None),
                    content_length=len(content or ""),
                    usage={
                        "completion_tokens": getattr(meta, "completion_tokens", None),
                        "reasoning_tokens": getattr(meta, "reasoning_tokens", None),
                        "prompt_tokens": getattr(meta, "prompt_tokens", None),
                    },
                    duration_ms=int((time.monotonic() - started) * 1000),
                ))
            return value
        except LLMError as exc:
            last_error = exc
            meta = getattr(client, "last_metadata", None)
            # 失败记录的元数据必须来自异常本身：LLMError 携带了失败那次响应的
            # content_length / reasoning_length / usage。硬编码 0 会掩盖
            # 「思考链吃光 16000 token、正文 0 字符」这类关键诊断信息。
            # 优先用异常自带的元数据；业务校验在 validate 回调里抛的异常
            # 没有 meta，此时回落到客户端记录的 last_metadata，
            # 否则「正文 3038 字符但 schema 不符」会被记成 content_length=0。
            exc_cl = int(getattr(exc, "content_length", 0) or 0)
            exc_rl = int(getattr(exc, "reasoning_length", 0) or 0)
            if exc_cl == 0 and meta is not None:
                exc_cl = int(getattr(meta, "content_length", 0) or 0)
            if exc_rl == 0 and meta is not None:
                exc_rl = int(getattr(meta, "reasoning_length", 0) or 0)
            exc_usage = dict(getattr(exc, "usage", None) or {})
            if not any(v for v in exc_usage.values()):
                exc_usage = {
                    "completion_tokens": getattr(meta, "completion_tokens", None),
                    "reasoning_tokens": getattr(meta, "reasoning_tokens", None),
                    "prompt_tokens": getattr(meta, "prompt_tokens", None),
                }
            record = AttemptRecord(
                attempt=attempt + 1, max_tokens=current_max_tokens,
                error_code=exc.code, error_message=exc.message,
                finish_reason=(getattr(exc, "finish_reason", None)
                               or getattr(meta, "finish_reason", None)),
                content_length=exc_cl,
                reasoning_length=exc_rl,
                usage=exc_usage,
                request_id=getattr(exc, "request_id", None),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if on_attempt is not None:
                on_attempt(record)

            if attempt >= retry_limit:
                raise

            # 输入超上下文属于配置类错误：重试、退避、加 token、改提示词都不可能
            # 让它通过，只会把一次明确失败拖成 N 次无谓请求。
            if exc.code == ERR_INPUT_TOO_LONG:
                raise

            # ---- 按错误码决定重试方式 ----
            if exc.code in _WIDEN_CODES:
                current_max_tokens = _next_max_tokens(
                    current_max_tokens, floor=widen_floor, hard=hard_max_tokens)
                if _TRUNCATE_HINT not in prompt:
                    prompt = prompt + _TRUNCATE_HINT
                continue

            if exc.code == ERR_EMPTY_CONTENT:
                # 空正文：首次原样重试（偶发空转），第二次起放大预算
                # （推理模型思考吃光 token 的典型表现）
                if empty_seen:
                    current_max_tokens = _next_max_tokens(
                        current_max_tokens, floor=widen_floor, hard=hard_max_tokens)
                empty_seen = True
                if _EMPTY_HINT not in prompt:
                    prompt = prompt + _EMPTY_HINT
                continue

            if exc.code in _REPAIR_PROMPT_CODES:
                hint = _SCHEMA_HINT if exc.code == ERR_SCHEMA_MISMATCH else _JSON_ONLY_HINT
                if hint not in prompt:
                    prompt = prompt + hint
                continue

            if exc.code in _BACKOFF_CODES or _is_rate_limited(exc.message):
                # 指数退避：1s, 2s, 4s…（上限 8s），避免把供应商压垮
                delay = min(2 ** attempt, 8)
                time.sleep(delay)
                continue

            # 未识别的错误码：不盲目重试
            raise

    raise last_error or LLMError(ERR_INVALID_OUTPUT, "解析重试耗尽")


def meta_from_client(client: LLMClient, *, content: str | None = None) -> LLMResponseMeta:
    """从 client.last_metadata 构造元数据（供上层记录与二次判定）。"""
    m = getattr(client, "last_metadata", None)
    return LLMResponseMeta(
        content=content,
        finish_reason=getattr(m, "finish_reason", None),
        usage={
            "completion_tokens": getattr(m, "completion_tokens", None),
            "reasoning_tokens": getattr(m, "reasoning_tokens", None),
            "prompt_tokens": getattr(m, "prompt_tokens", None),
        },
        model=getattr(m, "model", ""),
        max_tokens=getattr(m, "max_tokens", 0),
    )
