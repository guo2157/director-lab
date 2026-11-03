"""agent_governance 单元测试：预算控制与策略引擎（单 Agent 框架 P3）。"""
from app.services.agent_governance import (
    NextAction,
    PolicyDecision,
    TerminalStatus,
    ToolBudget,
    evaluate_tool_policy,
)


def test_budget_blocks_calls_after_limit():
    b = ToolBudget(max_calls=2, max_total_duration_ms=10_000, max_context_chars=1000)
    assert b.check_before("t1") is None
    b.record(100)
    assert b.check_before("t2") is None
    b.record(200)
    # 超过调用数上限：返回违规描述，调用方应跳过
    v = b.check_before("t3")
    assert v is not None and "max_calls" in v
    b.violations.append(v)
    assert len(b.violations) == 1


def test_budget_blocks_duration_after_limit():
    b = ToolBudget(max_calls=10, max_total_duration_ms=500, max_context_chars=1000)
    b.record(600)
    v = b.check_before("t")
    assert v is not None and "max_total_duration_ms" in v


def test_budget_context_limit():
    b = ToolBudget(max_calls=10, max_total_duration_ms=10_000, max_context_chars=100)
    assert b.context_allowed(60)
    b.record_context(60)
    assert not b.context_allowed(50)
    assert b.context_allowed(40)


def test_budget_audit_payload():
    b = ToolBudget()
    b.record(120)
    p = b.audit_payload()
    assert p["calls"] == 1
    assert p["total_duration_ms"] == 120
    assert p["violations"] == []


def test_policy_requires_confirmation_for_approval_tools():
    spec = {"requires_approval": True, "risk_level": "high"}
    assert evaluate_tool_policy(spec, approved=False) == PolicyDecision.REQUIRE_CONFIRMATION
    assert evaluate_tool_policy(spec, approved=True) == PolicyDecision.ALLOW


def test_policy_requires_confirmation_for_high_risk():
    spec = {"requires_approval": False, "risk_level": "high"}
    assert evaluate_tool_policy(spec, approved=False) == PolicyDecision.REQUIRE_CONFIRMATION
    assert evaluate_tool_policy(spec, approved=True) == PolicyDecision.ALLOW


def test_policy_allows_readonly_tools():
    spec = {"requires_approval": False, "risk_level": "low"}
    assert evaluate_tool_policy(spec, approved=False) == PolicyDecision.ALLOW


def test_enums_complete():
    # 终态与动作枚举完整性：编排层只允许从枚举集合选择
    assert TerminalStatus.COMPLETED == "COMPLETED"
    assert TerminalStatus.WAITING_FOR_USER == "WAITING_FOR_USER"
    assert TerminalStatus.DENIED_BY_POLICY == "DENIED_BY_POLICY"
    assert NextAction.ANSWER == "answer"
    assert NextAction.FAIL_SAFE == "fail_safe"
