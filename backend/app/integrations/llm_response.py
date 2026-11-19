"""LLM 响应完整性校验与 JSON 安全解析（唯一事实来源）。

设计目标：把「空输出 / 截断 / 流中断 / 供应商错误 / 非 JSON」从 json.loads 的
``Expecting value: line 1 column 1 (char 0)`` 这一个笼统异常里拆出来，按真实成因
分类，让重试策略能按类型决策（该加 token 的加 token，该修格式的修格式，
该退避的退避），而不是一律当截断放大预算。

纪律：
- 禁止把 reasoning_content 当 content 使用（DeepSeek 等推理模型的思考链不是答案）。
- 禁止空 content 直接进入 json.loads。
- 禁止空 content 被判成功。
- ``Expecting value`` 不再一律等同 Token 截断（空串、纯空白、纯文本都会抛它）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---- 错误码（对外稳定标识，供事件与前端展示） ----
ERR_EMPTY_CONTENT = "llm_empty_content"
ERR_OUTPUT_TRUNCATED = "llm_output_truncated"
ERR_STREAM_INTERRUPTED = "llm_stream_interrupted"
ERR_PROVIDER_ERROR = "llm_provider_error"
ERR_NON_JSON_OUTPUT = "llm_non_json_output"
ERR_INVALID_OUTPUT = "llm_invalid_output"
ERR_SCHEMA_MISMATCH = "llm_schema_mismatch"

#: 可重试的错误码 → 重试时的调整方向
#:   widen: 放大 max_tokens（仅当供应商真的遵守该参数时有效）
#:   repair: 追加「只输出 JSON」的格式修复提示
#:   backoff: 退避后原样重试（限流/超时/网络）
RETRYABLE_CODES: dict[str, str] = {
    ERR_OUTPUT_TRUNCATED: "widen",
    ERR_EMPTY_CONTENT: "widen_or_retry",
    ERR_STREAM_INTERRUPTED: "backoff",
    ERR_PROVIDER_ERROR: "backoff",
    ERR_NON_JSON_OUTPUT: "repair",
    ERR_INVALID_OUTPUT: "repair",
    ERR_SCHEMA_MISMATCH: "repair",
}


@dataclass
class LLMResponseMeta:
    """一次 LLM 调用的完整响应元数据（校验与重试的唯一输入）。"""

    content: str | None = None
    reasoning_content: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    stream_completed: bool = True
    provider_error: str | None = None
    model: str = ""
    max_tokens: int = 0

    # --- 派生字段（便于日志与事件） ---
    @property
    def content_length(self) -> int:
        return len(self.content or "")

    @property
    def reasoning_length(self) -> int:
        return len(self.reasoning_content or "")

    @property
    def completion_tokens(self) -> int | None:
        v = self.usage.get("completion_tokens")
        return int(v) if isinstance(v, (int, float)) else None

    @property
    def reasoning_tokens(self) -> int | None:
        v = self.usage.get("reasoning_tokens")
        return int(v) if isinstance(v, (int, float)) else None

    def summary(self) -> dict[str, Any]:
        """用于事件落库与日志的紧凑摘要（不含正文，避免日志膨胀）。"""
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "finish_reason": self.finish_reason,
            "content_length": self.content_length,
            "reasoning_length": self.reasoning_length,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "prompt_tokens": self.usage.get("prompt_tokens"),
            "total_tokens": self.usage.get("total_tokens"),
            "request_id": self.request_id,
            "stream_completed": self.stream_completed,
        }


@dataclass
class ValidationResult:
    valid: bool
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    retry_hint: str | None = None
    likely_truncated: bool = False
    finish_reason: str | None = None
    content_length: int = 0
    reasoning_length: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "retry_hint": self.retry_hint,
            "likely_truncated": self.likely_truncated,
            "finish_reason": self.finish_reason,
            "content_length": self.content_length,
            "reasoning_length": self.reasoning_length,
            "usage": self.usage,
        }


def _fail(meta: LLMResponseMeta, code: str, message: str, *,
          truncated: bool = False, retryable: bool | None = None) -> ValidationResult:
    hint = RETRYABLE_CODES.get(code)
    return ValidationResult(
        valid=False,
        error_code=code,
        error_message=message,
        retryable=bool(hint) if retryable is None else retryable,
        retry_hint=hint,
        likely_truncated=truncated,
        finish_reason=meta.finish_reason,
        content_length=meta.content_length,
        reasoning_length=meta.reasoning_length,
        usage=dict(meta.usage or {}),
    )


def validate_llm_response(meta: LLMResponseMeta) -> ValidationResult:
    """校验 LLM 响应是否具备「可用于业务解析」的完整性。

    判定顺序（顺序即优先级，不可调换）：
    1. 供应商明确报错 → llm_provider_error
    2. 流式请求未正常完成 → llm_stream_interrupted
    3. content 为 None → llm_empty_content
    4. content 去空白后为空 → llm_empty_content
    5. finish_reason=length → llm_output_truncated
    6. 其余 → 交给 JSON 校验（parse_json_safely）
    """
    # 1. 供应商错误优先：错误体里即使混入文本也不能当 JSON
    if meta.provider_error:
        return _fail(meta, ERR_PROVIDER_ERROR,
                     f"语言模型供应商返回错误: {str(meta.provider_error)[:300]}")

    # 2. 流中断：已收到部分内容也必须失败，不能把残缺结果当成功落库
    if not meta.stream_completed:
        return _fail(
            meta, ERR_STREAM_INTERRUPTED,
            f"语言模型流式响应未正常结束（已接收 {meta.content_length} 字符正文、"
            f"{meta.reasoning_length} 字符思考），连接可能中断",
        )

    # 3. / 4. 空正文：这是本次故障的直接成因，必须有独立错误码
    if meta.content is None:
        return _fail(
            meta, ERR_EMPTY_CONTENT,
            _empty_detail(meta, "content 字段为 None"),
        )
    if not meta.content.strip():
        return _fail(
            meta, ERR_EMPTY_CONTENT,
            _empty_detail(meta, f"content 为空白（{meta.content_length} 字符）"),
        )

    # 5. 显式截断标记
    if (meta.finish_reason or "").lower() == "length":
        return _fail(
            meta, ERR_OUTPUT_TRUNCATED,
            f"语言模型输出被截断（finish_reason=length，max_tokens={meta.max_tokens}，"
            f"已输出 {meta.content_length} 字符正文），请提高 token 预算或缩小单次解析范围",
            truncated=True,
        )

    return ValidationResult(
        valid=True,
        finish_reason=meta.finish_reason,
        content_length=meta.content_length,
        reasoning_length=meta.reasoning_length,
        usage=dict(meta.usage or {}),
    )


def _empty_detail(meta: LLMResponseMeta, reason: str) -> str:
    """空正文的详细诊断：区分「思考吃光预算」与「模型真的没吐字」。"""
    parts = [f"语言模型未返回正文（{reason}）", f"finish_reason={meta.finish_reason}"]
    if meta.reasoning_length:
        parts.append(
            f"但收到 {meta.reasoning_length} 字符思考内容"
            f"（reasoning_tokens={meta.reasoning_tokens}），"
            "说明模型把预算消耗在思考链上、未产出最终答案；"
            "思考内容不可代替正文解析"
        )
    if meta.completion_tokens is not None:
        parts.append(f"completion_tokens={meta.completion_tokens}")
    if meta.max_tokens:
        parts.append(f"请求 max_tokens={meta.max_tokens}")
    if meta.request_id:
        parts.append(f"request_id={meta.request_id}")
    return "，".join(parts)


# ------------------------------------------------ 供应商静默截断检测

#: 已知会静默截断并谎报 finish_reason=stop 的网关的 completion 硬上限。
#: 2026-11-18 对 http://212.64.16.68:3065/v1 实测：max_tokens 传 32768 / 65536 /
#: 131072，completion_tokens 恒等于 16000 且 finish_reason 恒为 stop，
#: 因此「靠 finish_reason=length 判截断」在该网关上永远不触发。
_KNOWN_SILENT_CAPS: dict[str, int] = {}

#: 判定「达到供应商硬上限」的相对容差：completion_tokens 落在上限的 [95%, 上限] 区间
_CAP_TOLERANCE = 0.95


def note_provider_cap(base_url: str, completion_tokens: int | None) -> None:
    """记录某端点观测到的 completion 上限（由调用方在拿到 usage 后调用）。"""
    if not base_url or not completion_tokens:
        return
    prev = _KNOWN_SILENT_CAPS.get(base_url)
    # 取观测到的最大值作为上限（同一网关不同请求可能因提前 stop 而更小）
    if prev is None or completion_tokens > prev:
        _KNOWN_SILENT_CAPS[base_url] = int(completion_tokens)


def get_provider_cap(base_url: str) -> int | None:
    return _KNOWN_SILENT_CAPS.get(base_url)


def is_provider_capped(meta: LLMResponseMeta, base_url: str = "") -> bool:
    """判断本次响应是否被供应商的隐藏上限截断（而非模型自然结束）。

    依据：``completion_tokens`` 已经顶到观测到的上限，但 ``finish_reason`` 仍是
    ``stop``——这是网关静默截断的典型特征。
    """
    cap = get_provider_cap(base_url) if base_url else None
    ct = meta.completion_tokens
    if cap is None or ct is None:
        return False
    return ct >= int(cap * _CAP_TOLERANCE)


def detect_truncation(meta: LLMResponseMeta, *, base_url: str = "",
                      json_error: str | None = None,
                      content: str | None = None) -> bool:
    """综合判定「输出是否被截断」，覆盖三种信号。

    1. finish_reason=length（标准信号，但供应商常不返回）
    2. completion_tokens 顶到供应商隐藏上限（静默截断）
    3. JSON 错误特征显示结构中途断开（Unterminated string 等）
    """
    if (meta.finish_reason or "").lower() == "length":
        return True
    if is_provider_capped(meta, base_url):
        # 顶到上限 + 正文未完整（JSON 解析失败或正文为空）才算截断
        if json_error is not None:
            return True
        if not (content or "").strip():
            return True
    if json_error:
        return is_likely_truncated_json(json_error, content)
    return False


# ---------------------------------------------------------------- JSON 解析

#: 明确的「输出被截断」特征。
#: 注意 ``Expecting value`` 的处理：它既可能来自**空内容**（char 0），也可能来自
#: **截断在「冒号后、值之前」**（如 ``"appearance": `` 被补齐成 ``"appearance": }``）。
#: 因此它只在「content 非空」时作为截断证据——空内容的判定由
#: ``validate_llm_response`` 前置拦截，不会走到这里。
_TRUNCATION_MARKERS = (
    "Unterminated string",
    "Unterminated array",
    "Unterminated object",
    "Expecting ',' delimiter",
    "Expecting ':' delimiter",
    "Expecting property name enclosed in double quotes",
    "Invalid \\uXXXX escape",
    "EOF while parsing",
)

#: 仅在「content 非空」时才视为截断证据的特征
_TRUNCATION_MARKERS_NONEMPTY_ONLY = (
    "Expecting value",
)


def is_likely_truncated_json(message: str, content: str | None = None) -> bool:
    """判断 JSON 解析失败是否真的由「输出中途被截断」引起。

    ``Expecting value`` 的归属：
    - content 为空/纯空白 → **不是**截断（应归 llm_empty_content，已由前置校验拦截）
    - content 非空且括号不平衡 → 是截断（典型：截断在冒号后、值之前）

    这一区分是本次修复的核心：旧实现无条件把 ``Expecting value`` 当截断，
    导致空内容被误判为「需要放大 token」；而完全移除它又会漏掉真实截断。
    """
    msg = message or ""
    if any(m in msg for m in _TRUNCATION_MARKERS):
        return True
    if any(m in msg for m in _TRUNCATION_MARKERS_NONEMPTY_ONLY):
        # 只在有实际内容时才认定截断
        if not (content or "").strip():
            return False
        stripped = (content or "").strip()
        # 括号/字符串未闭合 → 确认是截断；否则可能只是局部语法错误
        return not _brackets_balanced(stripped)
    return False


def _brackets_balanced(text: str) -> bool:
    stack: list[str] = []
    in_str = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return False
    return not stack and not in_str


_FENCE_RE = re.compile(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """剥离 markdown 代码块包裹（```json ... ``` 或 ``` ... ```）。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    m = _FENCE_RE.match(t)
    if m:
        return m.group(1).strip()
    lines = [ln for ln in t.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def _extract_json_span(text: str) -> str:
    """从「前后夹着普通文本」的输出里截取最外层 JSON 对象/数组。"""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        return text
    start = min(starts)
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def classify_json_error(raw: str | None, exc: Exception | None) -> str:
    """把 JSON 解析失败归到具体错误码（供重试策略决策）。"""
    if raw is None or not raw.strip():
        return ERR_EMPTY_CONTENT
    msg = str(exc or "")
    if is_likely_truncated_json(msg, raw):
        return ERR_OUTPUT_TRUNCATED
    # 完全不含 JSON 结构字符 → 模型输出了自然语言
    if not any(ch in raw for ch in "{["):
        return ERR_NON_JSON_OUTPUT
    return ERR_INVALID_OUTPUT


def parse_json_safely(raw: str | None, *, expect: type = dict,
                      meta: LLMResponseMeta | None = None,
                      base_url: str = "") -> tuple[Any | None, ValidationResult]:
    """安全解析 LLM 返回的 JSON，返回 ``(对象, 校验结果)``。

    覆盖：None / 空串 / 纯空白 / markdown 代码块 / 前后夹文本 / 语法错误 /
    截断 / 完整但顶层类型不符。

    与旧 ``parse_llm_json`` 的关键差异：空内容返回 ``llm_empty_content`` 而不是
    ``llm_invalid_output``；``Expecting value`` 不再被当作截断；传入 ``meta`` 时
    结合 ``completion_tokens`` 与供应商上限识别「静默截断」。
    """
    meta = meta or LLMResponseMeta(content=raw)
    base = validate_llm_response(meta)
    if not base.valid:
        return None, base

    text = _strip_code_fence(raw or "")
    if not text:
        return None, _fail(meta, ERR_EMPTY_CONTENT, "语言模型正文剥离代码块后为空")

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        # 先判定是否为截断：截断必须失败并触发重试，**绝不**补齐后当成功。
        # 补齐出来的 JSON 虽然语法合法，但缺字段（如只写了一半的 appearance），
        # 一旦落库就是残缺数据，比报错更糟。
        truncated = detect_truncation(
            meta, base_url=base_url, json_error=str(exc), content=text)
        if truncated:
            cap = get_provider_cap(base_url) if base_url else None
            cap_note = (
                f"；该端点 completion_tokens 已顶到观测上限 {cap}"
                f"（finish_reason 仍为 {meta.finish_reason}，属供应商静默截断）"
                if cap and is_provider_capped(meta, base_url) else ""
            )
            return None, _fail(
                meta, ERR_OUTPUT_TRUNCATED,
                f"语言模型输出 JSON 在中途截断（已输出 {len(text)} 字符，"
                f"max_tokens={meta.max_tokens}）{cap_note}：{exc}",
                truncated=True,
            )

        # 二级修复：前后夹普通文本时截取最外层结构（不是截断，只是包裹了说明文字）
        span = _extract_json_span(text)
        if span != text:
            try:
                obj = json.loads(span)
            except json.JSONDecodeError:
                obj = None
        else:
            obj = None

        if obj is None:
            code = classify_json_error(text, exc)
            if code == ERR_NON_JSON_OUTPUT:
                return None, _fail(
                    meta, ERR_NON_JSON_OUTPUT,
                    f"语言模型输出不是 JSON（前 120 字符：{text[:120]!r}）",
                )
            return None, _fail(
                meta, ERR_INVALID_OUTPUT, f"语言模型输出 JSON 语法错误: {exc}",
            )

    if expect is not None and not isinstance(obj, expect):
        return None, _fail(
            meta, ERR_SCHEMA_MISMATCH,
            f"语言模型输出 JSON 顶层类型为 {type(obj).__name__}，期望 {expect.__name__}",
        )

    return obj, ValidationResult(
        valid=True, content_length=len(raw or ""), usage=dict(meta.usage or {}),
    )


# ------------------------------------------------- 业务校验（结构非空约束）

def validate_parse_payload(data: dict) -> list[str]:
    """业务校验：JSON 能解析 ≠ 结果可用。

    返回问题列表（空列表表示通过）。这里只做「结构性缺失」判定，
    字段级细节由 ``normalize_parse_output`` 负责，避免两套规则漂移。
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["顶层不是对象"]

    scenes = data.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        problems.append("scenes 为空（未解析出任何场景）")
    else:
        shot_total = 0
        for idx, sc in enumerate(scenes):
            if not isinstance(sc, dict):
                problems.append(f"scenes[{idx}] 不是对象")
                continue
            title = (sc.get("title") or "").strip()
            if not title:
                problems.append(f"scenes[{idx}] 缺少 title")
            shots = sc.get("shots")
            if not isinstance(shots, list) or not shots:
                problems.append(f"场景「{title or idx}」无 shots")
            else:
                shot_total += len(shots)
                for s_idx, sh in enumerate(shots):
                    if not isinstance(sh, dict):
                        problems.append(f"场景「{title or idx}」shots[{s_idx}] 不是对象")
                        continue
                    # 镜号字段名以本项目归一化后的口径为准：seq（见
                    # script_parse.apply_parse_result 的 sh.get("seq")）。
                    # 兼容 shot_no / shot 是给外部/历史数据留的余量。
                    if not (sh.get("seq") or sh.get("shot_no") or sh.get("shot")):
                        problems.append(f"场景「{title or idx}」shots[{s_idx}] 缺少镜号")
        if shot_total == 0:
            problems.append("全部分镜数为 0（JSON 合法但无可用镜头）")

    episodes = data.get("episodes")
    if isinstance(episodes, list) and episodes:
        for idx, ep in enumerate(episodes):
            if not isinstance(ep, dict):
                problems.append(f"episodes[{idx}] 不是对象")
                continue
            try:
                no = int(ep.get("episode") or 0)
            except (TypeError, ValueError):
                no = 0
            if no < 1:
                problems.append(f"episodes[{idx}] 集号非法")

    chars = data.get("characters")
    if isinstance(chars, list):
        names = [(c.get("name") or "").strip() for c in chars if isinstance(c, dict)]
        if any(not n for n in names):
            problems.append("存在无 name 的角色")
        dup = {n for n in names if n and names.count(n) > 1}
        if dup:
            problems.append(f"角色名重复: {sorted(dup)}")

    return problems
