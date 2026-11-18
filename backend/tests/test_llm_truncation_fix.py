# LLM output validation and retry tests.
import json
import pytest

from app.integrations.llm import (
    LLMError, LLMCallMetadata, is_truncated_json_output,
    parse_llm_json, _repair_truncated_json,
)
from app.services.llm_output_validator import (
    OutputStatus, classify_llm_output, is_likely_truncated_json,
    is_error_near_end, parse_json_safely,
    parse_json_with_repair, validate_script_result,
)


class TestTruncationMarkers:
    def test_colon_delimiter(self):
        msg = "Expecting ':' delimiter: line 42 column 15"
        assert is_truncated_json_output(msg)
        assert is_likely_truncated_json(msg)

    def test_comma_delimiter(self):
        assert is_truncated_json_output("Expecting ',' delimiter")

    def test_unterminated_string(self):
        assert is_truncated_json_output("Unterminated string starting at")

    def test_eof_while_parsing(self):
        assert is_truncated_json_output("EOF while parsing")

    def test_unterminated_array(self):
        assert is_truncated_json_output("Unterminated array")

    def test_unterminated_object(self):
        assert is_truncated_json_output("Unterminated object")

    def test_normal_message_not_detected(self):
        assert not is_truncated_json_output("Connection timeout")
        assert not is_truncated_json_output("")


class TestClassifyOutput:
    def test_length_is_incomplete(self):
        r = classify_llm_output("{}", finish_reason="length")
        assert r.status == OutputStatus.INCOMPLETE
        assert r.likely_truncated is True
        assert r.retryable is True

    def test_stop_valid_is_success(self):
        r = classify_llm_output(
            json.dumps({"a": 1}), finish_reason="stop")
        assert r.status == OutputStatus.SUCCESS
        assert r.parsed_object == {"a": 1}

    def test_stop_truncated_near_end(self):
        content = json.dumps({"scenes": [{"name": "test"}]})
        content = content[:-1]  # cut last bracket
        r = classify_llm_output(content, finish_reason="stop",
                                output_length=len(content))
        assert r.retryable is True

    def test_aborted(self):
        r = classify_llm_output("{}", finish_reason="aborted")
        assert r.status == OutputStatus.INCOMPLETE
        assert r.retryable is True

    def test_content_filter(self):
        r = classify_llm_output("blocked", finish_reason="content_filter")
        assert r.status == OutputStatus.CONTENT_FILTERED
        assert r.retryable is False

    def test_insufficient_resource(self):
        r = classify_llm_output("p",
                                finish_reason="insufficient_system_resource")
        assert r.status == OutputStatus.INCOMPLETE
        assert r.retryable is True

    def test_with_usage(self):
        r = classify_llm_output(
            json.dumps({"ok": True}), finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 5000})
        assert r.metadata.get("usage")

    def test_stop_invalid_in_middle(self):
        pad = "x" * 5000
        content = "badjson" + pad
        r = classify_llm_output(content, finish_reason="stop",
                                output_length=len(content))
        assert r.likely_truncated is False


class TestErrorNearEnd:
    def test_at_tail(self):
        assert is_error_near_end("err", 990, 1000) is True

    def test_at_head(self):
        assert is_error_near_end("err", 10, 1000) is False

    def test_none_position(self):
        assert is_error_near_end("err", None, 1000) is False


class TestParseJsonSafely:
    def test_valid(self):
        assert parse_json_safely(json.dumps({"a": 1})) == {"a": 1}

    def test_invalid_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_json_safely("not json")


class TestRepairTruncated:
    def test_string_cut(self):
        repaired = _repair_truncated_json(
            json.dumps({"name": "zhang"})[:-1])
        assert repaired.endswith('"')

    def test_bracket_cut(self):
        repaired = _repair_truncated_json(
            json.dumps({"scenes": [{"n": "a"}]})[:-2])
        assert repaired.endswith("]}")

    def test_already_valid(self):
        valid = json.dumps({"a": 1})
        assert _repair_truncated_json(valid) == valid

    def test_empty(self):
        assert _repair_truncated_json("") == ""


class TestParseWithRepair:
    def test_recover(self):
        content = json.dumps({"a": 1, "b": [1, 2]})
        parsed, note = parse_json_with_repair(content[:-3])
        assert parsed is not None
        assert note is not None

    def test_already_valid(self):
        valid = json.dumps({"a": 1})
        parsed, note = parse_json_with_repair(valid)
        assert parsed == {"a": 1}
        assert note is None

    def test_unrecoverable(self):
        parsed, note = parse_json_with_repair("not json")
        assert parsed is None
        assert note is None


class TestParseLlmJson:
    def test_markdown_fence(self):
        result = parse_llm_json(
            json.dumps({"a": 1}))
        assert result == {"a": 1}

    def test_truncated_recovery(self):
        content = json.dumps({"a": 1, "b": ["x"]})
        result = parse_llm_json(content[:-3])
        assert result["a"] == 1

    def test_garbage_raises(self):
        with pytest.raises(LLMError):
            parse_llm_json("not json")


class TestValidateScriptResult:
    def test_valid(self):
        data = {
            "scenes": [{"shots": [{"shot_id": 1}]}],
            "characters": [{"name": "A"}],
            "props": [], "episodes": [{"episode": 1}],
        }
        assert validate_script_result(data)["valid"] is True

    def test_missing_scenes(self):
        r = validate_script_result(
            {"characters": [], "props": [], "episodes": []})
        assert r["valid"] is False

    def test_empty_scenes(self):
        r = validate_script_result(
            {"scenes": [], "characters": [],
             "props": [], "episodes": []})
        assert r["valid"] is False

    def test_no_shots(self):
        r = validate_script_result({
            "scenes": [{"name": "x", "shots": []}],
            "characters": [], "props": [], "episodes": [],
        })
        assert r["valid"] is False


class TestMetadata:
    def test_fields(self):
        meta = LLMCallMetadata(
            model="m", max_tokens=65536, finish_reason="stop",
            prompt_tokens=500, completion_tokens=3000,
            reasoning_tokens=12000, retry_count=1,
        )
        assert meta.model == "m"
        assert meta.reasoning_tokens == 12000

    def test_defaults(self):
        meta = LLMCallMetadata()
        assert meta.reasoning_tokens is None
        assert meta.retry_count == 0
