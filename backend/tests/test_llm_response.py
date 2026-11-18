"""LLM 响应完整性与 JSON 安全解析测试。

覆盖需求清单 §10 的 17 类场景。全部使用真实对象与真实 json 模块，
不 mock json.loads——要验证的就是分类逻辑本身。
"""
from __future__ import annotations

import json

import pytest

from app.integrations.llm import LLMError, parse_llm_json
from app.integrations.llm_response import (
    ERR_EMPTY_CONTENT,
    ERR_INVALID_OUTPUT,
    ERR_NON_JSON_OUTPUT,
    ERR_OUTPUT_TRUNCATED,
    ERR_PROVIDER_ERROR,
    ERR_SCHEMA_MISMATCH,
    ERR_STREAM_INTERRUPTED,
    LLMResponseMeta,
    classify_json_error,
    detect_truncation,
    is_likely_truncated_json,
    note_provider_cap,
    parse_json_safely,
    validate_llm_response,
    validate_parse_payload,
)

BASE_URL = "http://gateway.test/v1"

VALID_JSON = '{"scenes": [{"title": "楼顶", "shots": [{"shot_no": 1}]}], "characters": []}'


def _meta(**kw) -> LLMResponseMeta:
    kw.setdefault("stream_completed", True)
    return LLMResponseMeta(**kw)


# ══════════════════════════════════════════════ 1-3. 空内容三种形态

@pytest.mark.parametrize("content", [None, "", "   ", "\n\t  \r\n"])
def test_empty_content_is_classified_as_empty_not_invalid_json(content):
    """空 content 必须归 llm_empty_content，不能落到 json 解析。"""
    result = validate_llm_response(_meta(content=content))
    assert result.valid is False
    assert result.error_code == ERR_EMPTY_CONTENT
    assert result.retryable is True


def test_none_content_does_not_reach_json_loads():
    obj, result = parse_json_safely(None)
    assert obj is None
    assert result.error_code == ERR_EMPTY_CONTENT
    # 关键：不是 llm_invalid_output
    assert result.error_code != ERR_INVALID_OUTPUT


def test_blank_content_error_message_reports_length():
    result = validate_llm_response(_meta(content="     "))
    assert "空白" in (result.error_message or "")
    assert result.content_length == 5


# ══════════════════════════════════════════════ 4. reasoning 有内容但 content 为空

def test_reasoning_present_but_content_empty_is_still_empty_error():
    """思考链不能代替正文；且错误信息要指出「预算被思考吃光」。"""
    meta = _meta(
        content="",
        reasoning_content="我需要先分析这段剧本的人物关系……" * 20,
        finish_reason="stop",
        usage={"completion_tokens": 16000, "reasoning_tokens": 11000},
        max_tokens=32768,
    )
    result = validate_llm_response(meta)
    assert result.error_code == ERR_EMPTY_CONTENT
    assert result.reasoning_length > 0
    msg = result.error_message or ""
    assert "思考" in msg
    assert "不可代替正文" in msg
    assert "reasoning_tokens=11000" in msg


def test_reasoning_is_never_used_as_json_source():
    """即使 reasoning 里含合法 JSON，也不能被拿去解析。"""
    meta = _meta(content="", reasoning_content=VALID_JSON, finish_reason="stop")
    obj, result = parse_json_safely(meta.content, meta=meta)
    assert obj is None
    assert result.error_code == ERR_EMPTY_CONTENT


# ══════════════════════════════════════════════ 5-6. finish_reason

def test_finish_reason_length_is_truncated():
    result = validate_llm_response(_meta(content='{"a": 1', finish_reason="length"))
    assert result.error_code == ERR_OUTPUT_TRUNCATED
    assert result.likely_truncated is True


def test_finish_reason_stop_but_empty_content_is_empty_not_truncated():
    """stop + 空正文 → 空内容（检查提取链路），不是截断。"""
    result = validate_llm_response(_meta(content="", finish_reason="stop"))
    assert result.error_code == ERR_EMPTY_CONTENT
    assert result.likely_truncated is False


# ══════════════════════════════════════════════ 7-10. 流式场景

def test_stream_with_only_reasoning_chunks_fails_as_empty():
    """只收到 reasoning chunk 就结束：content 为空。"""
    meta = _meta(content="", reasoning_content="思考" * 100,
                 finish_reason="stop", stream_completed=True)
    assert validate_llm_response(meta).error_code == ERR_EMPTY_CONTENT


def test_stream_interrupted_after_content_is_not_success():
    """已收到部分正文但连接中断：必须失败，不能当成功。"""
    meta = _meta(content='{"scenes": [{"title": "楼', finish_reason=None,
                 stream_completed=False)
    result = validate_llm_response(meta)
    assert result.valid is False
    assert result.error_code == ERR_STREAM_INTERRUPTED
    assert result.content_length > 0


def test_stream_completed_but_empty_content():
    meta = _meta(content="", finish_reason="stop", stream_completed=True)
    assert validate_llm_response(meta).error_code == ERR_EMPTY_CONTENT


def test_stream_multi_chunk_full_json_parses():
    chunks = ['{"scenes"', ': [{"title"', ': "楼顶",', ' "shots": []}]}']
    content = "".join(chunks)
    meta = _meta(content=content, finish_reason="stop",
                 usage={"completion_tokens": 50})
    obj, result = parse_json_safely(content, meta=meta)
    assert result.valid is True
    assert obj["scenes"][0]["title"] == "楼顶"


# ══════════════════════════════════════════════ 11-13. 非 JSON / 代码块 / 截断

def test_plain_text_output_is_non_json():
    content = "抱歉，我需要更多信息才能解析这段剧本。"
    obj, result = parse_json_safely(content, meta=_meta(content=content))
    assert obj is None
    assert result.error_code == ERR_NON_JSON_OUTPUT


def test_markdown_code_fence_is_unwrapped():
    content = '```json\n{"scenes": []}\n```'
    obj, result = parse_json_safely(content, meta=_meta(content=content))
    assert result.valid is True
    assert obj == {"scenes": []}


def test_json_wrapped_in_prose_is_extracted():
    content = '好的，以下是解析结果：\n{"scenes": []}\n希望对你有帮助。'
    obj, result = parse_json_safely(content, meta=_meta(content=content))
    assert result.valid is True
    assert obj == {"scenes": []}


def test_truncated_json_is_reported_as_truncated_not_repaired():
    """截断必须失败——补齐后的残缺 JSON 不允许当成功落库。"""
    content = '{"scenes": [{"title": "楼顶", "shots": [{"shot_no": 1, "appearance": '
    obj, result = parse_json_safely(content, meta=_meta(content=content, finish_reason="stop"))
    assert obj is None
    assert result.error_code == ERR_OUTPUT_TRUNCATED
    assert result.likely_truncated is True


def test_unterminated_string_is_truncated():
    content = '{"scenes": [{"title": "楼顶的纸飞'
    obj, result = parse_json_safely(content, meta=_meta(content=content))
    assert obj is None
    assert result.error_code == ERR_OUTPUT_TRUNCATED


def test_expecting_value_with_empty_content_is_not_truncation():
    """空内容抛的 Expecting value (char 0) 不能判成截断。"""
    assert is_likely_truncated_json(
        "Expecting value: line 1 column 1 (char 0)", "") is False


def test_expecting_value_with_nonempty_unbalanced_is_truncation():
    """用户报的 line 83 column 15 (char 2990)：非空 + 括号不平衡 = 真截断。"""
    content = '{\n' + ",\n".join(f'  "k{i}": "v{i}"' for i in range(80)) + ',\n  "appearance": '
    assert is_likely_truncated_json(
        "Expecting value: line 83 column 15 (char 2990)", content) is True


# ══════════════════════════════════════════════ 14. 供应商错误

def test_provider_error_is_not_parsed_as_json():
    meta = _meta(content=None, provider_error="HTTP 429 rate limit exceeded")
    result = validate_llm_response(meta)
    assert result.error_code == ERR_PROVIDER_ERROR
    assert result.retryable is True


# ══════════════════════════════════════════════ 供应商静默截断

def test_silent_provider_cap_detected_via_usage():
    """网关硬截断 16000 却谎报 stop——靠 completion_tokens 识别。"""
    note_provider_cap(BASE_URL, 16000)
    content = '{"scenes": [{"title": "楼'
    meta = _meta(content=content, finish_reason="stop",
                 usage={"completion_tokens": 16000, "reasoning_tokens": 11000},
                 max_tokens=65536)
    assert detect_truncation(meta, base_url=BASE_URL, json_error="Unterminated string") is True
    obj, result = parse_json_safely(content, meta=meta, base_url=BASE_URL)
    assert result.error_code == ERR_OUTPUT_TRUNCATED
    assert "16000" in (result.error_message or "")


def test_cap_not_triggered_below_threshold():
    note_provider_cap(BASE_URL, 16000)
    content = '{"scenes": [{"title": "楼'
    meta = _meta(content=content, finish_reason="stop",
                 usage={"completion_tokens": 500}, max_tokens=32768)
    # 未顶到上限：仅靠 JSON 特征判定，仍是截断（Unterminated string）
    assert detect_truncation(meta, base_url=BASE_URL,
                             json_error="Unterminated string") is True
    # 但若 JSON 是完整合法的，顶不顶上限都不算截断
    assert detect_truncation(
        _meta(content=VALID_JSON, finish_reason="stop",
              usage={"completion_tokens": 500}), base_url=BASE_URL) is False


# ══════════════════════════════════════════════ Schema / 业务校验

def test_top_level_array_is_schema_mismatch():
    content = '[{"title": "楼顶"}]'
    obj, result = parse_json_safely(content, expect=dict, meta=_meta(content=content))
    assert obj is None
    assert result.error_code == ERR_SCHEMA_MISMATCH


def test_business_validation_rejects_empty_scenes():
    problems = validate_parse_payload({"scenes": [], "characters": []})
    assert any("scenes 为空" in p for p in problems)


def test_business_validation_rejects_scene_without_shots():
    problems = validate_parse_payload({"scenes": [{"title": "楼顶", "shots": []}]})
    assert any("无 shots" in p for p in problems)


def test_business_validation_accepts_minimal_valid_payload():
    payload = {
        "scenes": [{"title": "楼顶", "shots": [{"shot_no": 1}]}],
        "characters": [{"name": "小满"}],
        "episodes": [{"episode": 1}],
    }
    assert validate_parse_payload(payload) == []


# ══════════════════════════════════════════════ parse_llm_json 错误码透传

def test_parse_llm_json_raises_empty_code_for_blank():
    with pytest.raises(LLMError) as ei:
        parse_llm_json("")
    assert ei.value.code == ERR_EMPTY_CONTENT


def test_parse_llm_json_raises_truncated_code():
    with pytest.raises(LLMError) as ei:
        parse_llm_json('{"a": [1, 2')
    assert ei.value.code == ERR_OUTPUT_TRUNCATED


def test_parse_llm_json_accepts_valid():
    assert parse_llm_json('{"a": 1}') == {"a": 1}


# ══════════════════════════════════════════════ classify_json_error

@pytest.mark.parametrize("raw,expected", [
    (None, ERR_EMPTY_CONTENT),
    ("", ERR_EMPTY_CONTENT),
    ("   ", ERR_EMPTY_CONTENT),
])
def test_classify_json_error_empty(raw, expected):
    assert classify_json_error(raw, ValueError("Expecting value")) == expected


def test_classify_json_error_plain_text():
    assert classify_json_error("这是自然语言回复", ValueError("Expecting value")) == ERR_NON_JSON_OUTPUT


def test_business_validation_accepts_project_seq_shot_number():
    """本项目镜头号字段是 seq（apply_parse_result 用 sh.get("seq")）。

    校验器只认 shot_no/shot 会把正常结果全判为「缺少镜号」——
    这是接入保存路径时踩到的真实回归。
    """
    payload = {
        "scenes": [{"title": "教室", "shots": [
            {"seq": 1, "dialogue": "台词"}, {"seq": 2, "dialogue": "台词2"},
        ]}],
        "characters": [{"name": "林晓"}],
        "episodes": [{"episode": 1}],
    }
    assert validate_parse_payload(payload) == []


def test_business_validation_rejects_scenes_all_empty_shot_arrays():
    """真实故障复现：场景 7 个 / 分镜 0 个，必须判失败。"""
    payload = {
        "scenes": [{"title": f"场景{i}", "shots": []} for i in range(1, 8)],
        "characters": [{"name": "林晓"}],
        "episodes": [{"episode": 1}],
    }
    problems = validate_parse_payload(payload)
    assert len([p for p in problems if "无 shots" in p]) == 7
    assert any("全部分镜数为 0" in p for p in problems)
