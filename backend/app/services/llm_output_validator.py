"""LLM 输出统一校验模块。

提供模型输出完整性判断、JSON 解析和 Schema/业务校验的统一入口，
避免各处分散重复的异常字符串匹配逻辑。
"""
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OutputStatus(str, Enum):
    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    LIKELY_TRUNCATED = "likely_truncated"
    INVALID_JSON = "invalid_json"
    SCHEMA_INVALID = "schema_invalid"
    BUSINESS_INVALID = "business_invalid"
    CONTENT_FILTERED = "content_filtered"
    PROVIDER_ERROR = "provider_error"


@dataclass
class OutputClassification:
    status: OutputStatus
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    likely_truncated: bool = False
    finish_reason: str | None = None
    error_position: int | None = None
    remaining_characters: int = 0
    parsed_object: dict | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_TRUNCATION_MARKERS = (
    "Unterminated string",
    "Expecting ',' delimiter",
    "Expecting ':' delimiter",
    "Expecting property name enclosed in double quotes",
    "Expecting value",
    "Invalid \\uXXXX escape",
    "EOF while parsing",
    "Unterminated array",
    "Unterminated object",
    "Extra data",
)


def is_likely_truncated_json(error_message: str) -> bool:
    return any(marker in (error_message or "") for marker in _TRUNCATION_MARKERS)


def is_error_near_end(error_message: str, position: int | None, output_length: int) -> bool:
    if position is None or output_length <= 0:
        return False
    distance_from_end = output_length - position
    return distance_from_end <= max(200, int(output_length * 0.1))


def _strip_markdown_fence(text: str) -> str:
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.split(chr(10))
        lines = [l for l in lines if not l.strip().startswith(fence)]
        return chr(10).join(lines).strip()
    return text


def parse_json_safely(content: str) -> dict:
    text = _strip_markdown_fence(content.strip())
    return json.loads(text)


def _repair_truncated(text: str) -> str:
    if not text:
        return text
    bracket_stack = []
    in_str = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == chr(92):
            escaped = True
            continue
        if ch == chr(34):
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == chr(123):
            bracket_stack.append(chr(125))
        elif ch == chr(91):
            bracket_stack.append(chr(93))
        elif ch in (chr(125), chr(93)):
            if bracket_stack and bracket_stack[-1] == ch:
                bracket_stack.pop()
    suffix = ""
    if in_str:
        suffix += chr(34)
    while bracket_stack:
        suffix += bracket_stack.pop()
    return text + suffix if suffix else text


def parse_json_with_repair(content: str):
    text = _strip_markdown_fence(content.strip())
    try:
        return (json.loads(text), None)
    except json.JSONDecodeError:
        repaired = _repair_truncated(text)
        if repaired != text:
            try:
                return (json.loads(repaired), "JSON truncated and repaired")
            except json.JSONDecodeError:
                pass
        return (None, None)


def classify_llm_output(content, finish_reason, output_length=None, usage=None):
    ol = len(content)
    meta = {"output_length": ol}
    if usage:
        meta["usage"] = usage
    if finish_reason == "length":
        return OutputClassification(
            status=OutputStatus.INCOMPLETE,
            error_code="output_truncated_length",
            error_message="finish_reason=length, output truncated",
            retryable=True, likely_truncated=True,
            finish_reason="length",
            remaining_characters=ol, metadata=meta,
        )
    if finish_reason == "content_filter":
        return OutputClassification(
            status=OutputStatus.CONTENT_FILTERED,
            error_code="content_filtered",
            error_message="output filtered",
            retryable=False, likely_truncated=False,
            finish_reason="content_filter", metadata=meta,
        )
    if finish_reason in ("aborted", "insufficient_system_resource"):
        return OutputClassification(
            status=OutputStatus.INCOMPLETE,
            error_code="output_" + finish_reason,
            error_message="output incomplete: " + finish_reason,
            retryable=True, likely_truncated=True,
            finish_reason=finish_reason, metadata=meta,
        )
    try:
        parsed = parse_json_safely(content)
    except json.JSONDecodeError as e:
        pos = getattr(e, "pos", None)
        msg = str(e)
        near_end = is_error_near_end(msg, pos, ol) if pos is not None else False
        is_trunc = is_likely_truncated_json(msg)
        if near_end or is_trunc:
            return OutputClassification(
                status=OutputStatus.LIKELY_TRUNCATED,
                error_code="json_truncated",
                error_message="JSON likely truncated: " + msg,
                retryable=True, likely_truncated=True,
                finish_reason=finish_reason,
                error_position=pos,
                remaining_characters=ol - pos if pos else ol,
                metadata={**meta, "json_error": msg, "position": pos},
            )
        else:
            return OutputClassification(
                status=OutputStatus.INVALID_JSON,
                error_code="json_invalid",
                error_message="JSON invalid: " + msg,
                retryable=False, likely_truncated=False,
                finish_reason=finish_reason,
                error_position=pos,
                metadata={**meta, "json_error": msg, "position": pos},
            )
    if not isinstance(parsed, dict):
        return OutputClassification(
            status=OutputStatus.INVALID_JSON,
            error_code="json_not_dict",
            error_message="LLM output top-level must be object",
            retryable=False, likely_truncated=False,
            finish_reason=finish_reason, metadata=meta,
        )
    return OutputClassification(
        status=OutputStatus.SUCCESS,
        error_code="", error_message="",
        retryable=False, likely_truncated=False,
        finish_reason=finish_reason,
        parsed_object=parsed,
        remaining_characters=ol, metadata=meta,
    )


def validate_script_result(parsed: dict) -> dict:
    issues = []
    for k in ("scenes", "characters", "props", "episodes"):
        if k not in parsed:
            issues.append("missing key: " + k)
    scenes = parsed.get("scenes") or []
    if not isinstance(scenes, list):
        issues.append("scenes must be array")
    elif len(scenes) == 0:
        issues.append("scenes is empty")
    else:
        total_shots = 0
        for i, sc in enumerate(scenes):
            if not isinstance(sc, dict):
                issues.append("scenes[" + str(i) + "] is not object")
                continue
            shots = sc.get("shots") or []
            if not shots:
                issues.append("scenes[" + str(i) + "] has no shots")
            total_shots += len(shots)
        if total_shots == 0:
            issues.append("all scenes have no shots")
    return {"valid": len(issues) == 0, "issues": issues}
