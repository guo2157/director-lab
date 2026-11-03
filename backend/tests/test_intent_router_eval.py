"""意图路由离线评估集回归测试（版本化，tests/evals/intent_router_eval_v1.json）。

用途：任何意图路由规则/关键词/优先级变更后，本测试必须全量通过；
失败用例按 case id 定位，修复规则或同步更新评估集版本。
只覆盖确定性规则判定（apply_fast_path_rules），不依赖 LLM，可离线运行。
"""
import json
from pathlib import Path

import pytest

from app.services.intent_router import apply_fast_path_rules, route_action

EVAL_FILE = Path(__file__).parent / "evals" / "intent_router_eval_v1.json"


def _load_cases() -> list[dict]:
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    return data["cases"]


CASES = _load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_eval_case(case: dict):
    """逐条校验：期望意图/动作/风险标签与规则快路径判定一致。"""
    result = apply_fast_path_rules(case["input"])
    expected_intent = case.get("expected_intent")
    if expected_intent is None:
        # 未命中规则：回落 LLM，规则快路径应返回 None
        assert result is None, (
            f"[{case['id']}] {case['input']!r} 不应命中规则快路径，实际判定为 {result.intent if result else None}"
        )
        return
    assert result is not None, f"[{case['id']}] {case['input']!r} 应命中规则快路径"
    assert result.intent == expected_intent, (
        f"[{case['id']}] {case['input']!r} 意图应为 {expected_intent}，实际 {result.intent}"
    )
    expected_action = case.get("expected_action")
    if expected_action is not None:
        assert route_action(result.intent) == expected_action, (
            f"[{case['id']}] {case['input']!r} 动作应为 {expected_action}，"
            f"实际 {route_action(result.intent)}"
        )
    expected_risk = case.get("risk")
    if expected_risk is not None:
        assert result.risk_level == expected_risk, (
            f"[{case['id']}] {case['input']!r} 风险应为 {expected_risk}，实际 {result.risk_level}"
        )
    expected_pipeline = case.get("pipeline_action")
    if expected_pipeline is not None:
        entities = result.entities or {}
        assert entities.get("pipeline_action") == expected_pipeline, (
            f"[{case['id']}] {case['input']!r} 流程动作应为 {expected_pipeline}，"
            f"实际 {entities.get('pipeline_action')}"
        )


def test_eval_file_versioned():
    """评估集必须携带版本号：无版本的评估集不可追溯，禁止合入。"""
    data = json.loads(EVAL_FILE.read_text(encoding="utf-8"))
    assert data.get("version"), "评估集缺少 version 字段"
    assert data.get("cases"), "评估集 cases 为空"
