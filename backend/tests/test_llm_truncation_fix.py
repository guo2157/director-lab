"""LLM 输出解析与截断处理契约测试。

本文件锁的是 ``app/integrations/llm_response.py``（唯一生产实现）的真实行为，
经由 ``app/integrations/llm.py`` 的薄封装暴露。

核心不变量（**不允许被"优化"掉**）：

    截断的 JSON 必须报错并触发上层重试，绝不允许补齐后当成功返回。
    补齐出来的 JSON 语法合法但会缺字段（例如只写了一半的 appearance），
    一旦落库就是残缺数据，比直接报错更糟。

历史上这里测的是 ``app/services/llm_output_validator.py``——那是 08377bd
提交引入的并行实现，从未接入任何生产路径，且与上条不变量相反（会补齐）。
该模块已删除，测试同步改写。
"""
import json

import pytest

from app.integrations.llm import (
    LLMError,
    LLMCallMetadata,
    is_truncated_json_output,
    parse_llm_json,
)
from app.integrations.llm_response import (
    ERR_EMPTY_CONTENT,
    ERR_NON_JSON_OUTPUT,
    ERR_OUTPUT_TRUNCATED,
    classify_json_error,
    is_likely_truncated_json,
    parse_json_safely,
    validate_parse_payload,
)


class TestTruncationMarkers:
    """截断判定：只认 JSON 结构在中途断掉的信号。"""

    @pytest.mark.parametrize("msg", [
        "Expecting ':' delimiter: line 42 column 15",
        "Expecting ',' delimiter: line 7 column 3",
        "Unterminated string starting at: line 1 column 16",
        "EOF while parsing a list",
        "Unterminated array",
        "Unterminated object",
    ])
    def test_structure_markers_detected(self, msg):
        assert is_truncated_json_output(msg) is True
        assert is_likely_truncated_json(msg) is True

    @pytest.mark.parametrize("msg", [
        "Connection timeout",
        "",
    ])
    def test_non_structure_not_detected(self, msg):
        assert is_truncated_json_output(msg) is False

    def test_expecting_value_alone_is_not_truncation(self):
        """``Expecting value`` 是空串/纯自然语言也会抛的错，不能当截断证据。"""
        assert not is_likely_truncated_json("Expecting value: line 1 column 1")


class TestParseJsonSafelyContract:
    """``parse_json_safely`` 在各输入形态下的返回值。"""

    def test_valid_object(self):
        obj, res = parse_json_safely(json.dumps({"a": 1}), expect=dict)
        assert res.valid is True
        assert obj == {"a": 1}

    def test_markdown_fence_stripped(self):
        obj, res = parse_json_safely('```json\n{"a": 1}\n```', expect=dict)
        assert res.valid is True and obj == {"a": 1}

    def test_prose_around_object_extracted(self):
        """前后夹说明文字时截取最外层结构——这是包裹，不是截断。"""
        obj, res = parse_json_safely('结果如下：{"a": 1} 以上', expect=dict)
        assert res.valid is True and obj == {"a": 1}

    def test_empty_content_has_own_code(self):
        _, res = parse_json_safely("", expect=dict)
        assert res.valid is False
        assert res.error_code == ERR_EMPTY_CONTENT

    def test_whitespace_only_is_empty(self):
        _, res = parse_json_safely("   \n\t ", expect=dict)
        assert res.valid is False
        assert res.error_code == ERR_EMPTY_CONTENT

    def test_natural_language_gets_non_json_code(self):
        _, res = parse_json_safely("抱歉，我无法完成这个任务。", expect=dict)
        assert res.valid is False
        assert res.error_code == ERR_NON_JSON_OUTPUT

    def test_top_level_type_mismatch(self):
        obj, res = parse_json_safely("[1, 2, 3]", expect=dict)
        assert obj is None and res.valid is False


class TestTruncationIsNeverRepaired:
    """回归护栏：任何截断形态都不得"修复成功"。"""

    @pytest.mark.parametrize("raw,label", [
        (json.dumps({"a": 1, "b": ["x"]})[:-3], "字符串中途截断"),
        (json.dumps({"name": "zhang"})[:-1], "缺末尾对象括号"),
        (json.dumps({"scenes": [{"n": "a"}]})[:-2], "缺数组与对象括号"),
        ('{"appearance": ', "冒号后无值"),
        ('{"a": 1,', "逗号后截断"),
        ('{"shots": [', "数组开头截断"),
    ])
    def test_returns_error_not_partial_object(self, raw, label):
        obj, res = parse_json_safely(raw, expect=dict)
        assert obj is None, f"{label} 被补齐成 {obj!r}，会落库残缺数据"
        assert res.valid is False
        assert res.error_code == ERR_OUTPUT_TRUNCATED, f"{label} 错误码为 {res.error_code}"

    def test_percent_of_payload_never_leaks_partial_keys(self):
        """即使截断点靠后，也不能返回"看起来完整"的半截对象。"""
        payload = json.dumps({
            "project_analysis": {"summary": "x" * 200},
            "characters": [{"name": "林晓", "appearance": "短发"}] * 20,
        })
        obj, res = parse_json_safely(payload[:-40], expect=dict)
        assert obj is None and res.valid is False


class TestParseLlmJsonWrapper:
    """``llm.parse_llm_json`` 薄封装：成功后返回 dict，失败抛 LLMError。"""

    def test_valid_returns_dict(self):
        assert parse_llm_json(json.dumps({"a": 1})) == {"a": 1}

    def test_truncated_raises_with_truncation_code(self):
        content = json.dumps({"a": 1, "b": ["x"]})
        with pytest.raises(LLMError) as exc:
            parse_llm_json(content[:-3])
        assert exc.value.code == ERR_OUTPUT_TRUNCATED

    def test_truncated_error_carries_content_for_retry(self):
        """重试逻辑要靠 content_length 区分"拿到正文但不合法"与"没拿到正文"。"""
        content = json.dumps({"a": 1, "b": ["x"]})
        with pytest.raises(LLMError) as exc:
            parse_llm_json(content[:-3])
        assert exc.value.content_length == len(content) - 3

    def test_garbage_raises(self):
        with pytest.raises(LLMError):
            parse_llm_json("not json at all")


class TestClassifyJsonError:
    """错误归类：决定上层是否值得重试。"""

    def test_truncated_json_error_maps_to_truncated(self):
        try:
            json.loads('{"a": 1,')
        except json.JSONDecodeError as exc:
            assert classify_json_error('{"a": 1,', exc) == ERR_OUTPUT_TRUNCATED

    def test_natural_language_maps_to_non_json(self):
        try:
            json.loads("抱歉我做不到")
        except json.JSONDecodeError as exc:
            assert classify_json_error("抱歉我做不到", exc) == ERR_NON_JSON_OUTPUT


class TestValidateParsePayload:
    """业务校验：JSON 合法 ≠ 解析结果可用。"""

    def _ok(self):
        return {
            "scenes": [{"title": "教室", "shots": [{"seq": 1}]}],
            "characters": [{"name": "林晓"}],
            "episodes": [{"episode": 1}],
        }

    def test_valid_payload_has_no_problems(self):
        assert validate_parse_payload(self._ok()) == []

    def test_missing_scenes(self):
        data = self._ok()
        data.pop("scenes")
        assert any("scenes" in p for p in validate_parse_payload(data))

    def test_empty_scenes(self):
        data = self._ok()
        data["scenes"] = []
        assert any("scenes" in p for p in validate_parse_payload(data))

    def test_scene_without_shots(self):
        data = self._ok()
        data["scenes"] = [{"title": "教室", "shots": []}]
        assert any("shots" in p for p in validate_parse_payload(data))

    def test_shot_without_seq(self):
        data = self._ok()
        data["scenes"] = [{"title": "教室", "shots": [{"dialogue": "x"}]}]
        assert any("镜号" in p for p in validate_parse_payload(data))

    def test_scene_without_title(self):
        data = self._ok()
        data["scenes"] = [{"title": "", "shots": [{"seq": 1}]}]
        assert any("title" in p for p in validate_parse_payload(data))

    def test_duplicate_character_names(self):
        data = self._ok()
        data["characters"] = [{"name": "林晓"}, {"name": "林晓"}]
        assert any("重复" in p for p in validate_parse_payload(data))

    def test_character_without_name(self):
        data = self._ok()
        data["characters"] = [{"name": ""}]
        assert any("name" in p for p in validate_parse_payload(data))

    def test_non_object_toplevel(self):
        from typing import cast
        assert validate_parse_payload(cast(dict, [])) != []


class TestMetadata:
    def test_fields(self):
        meta = LLMCallMetadata(
            model="m", max_tokens=65536, finish_reason="stop",
            prompt_tokens=500, completion_tokens=3000,
            reasoning_tokens=12000, retry_count=1,
        )
        assert meta.model == "m"
        assert meta.reasoning_tokens == 12000
        assert meta.retry_count == 1

    def test_defaults(self):
        meta = LLMCallMetadata()
        assert meta.reasoning_tokens is None
        assert meta.retry_count == 0
