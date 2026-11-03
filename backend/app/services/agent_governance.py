"""Agent 治理模块：终态定义、工具预算控制与策略引擎（单 Agent 框架 P3）。

设计约束（对齐通用 Agent 开发指南）：
- 模型提出动作，策略系统决定动作是否允许（Prompt 引导，Policy 阻止）
- 硬性预算：max_tool_calls / max_total_duration_ms / max_context_chars，
  超限安全降级（跳过后续工具调用），不无限执行
- 终态显式枚举，不依赖自然语言判断
- 决策审计：结构化记录（工具名/参数摘要/耗时/违规原因），不记录隐藏推理链
"""
from dataclasses import dataclass, field
from enum import StrEnum


class NextAction(StrEnum):
    """下一动作枚举：编排层只允许从该集合选择，不解析自然语言猜动作。

    通用动作（对齐 Agent 开发指南）：
    - ANSWER：正常回答用户
    - RETRIEVE：检索补充上下文
    - CALL_TOOL：调用受控工具
    - ASK_USER：向用户澄清/确认
    - COMPLETE：任务完成
    - FAIL_SAFE：安全降级

    导演台专用动作（route_action 返回值）：
    - CHAT：进入主对话流程
    - REJECT：拒绝不安全请求
    - GUIDE：引导到对应页面（生成类操作不在对话中执行）
    - PIPELINE：流程节点操作（经状态机门禁）
    - CLARIFY：信息不足，向用户澄清
    """

    ANSWER = "answer"
    RETRIEVE = "retrieve"
    CALL_TOOL = "call_tool"
    ASK_USER = "ask_user"
    COMPLETE = "complete"
    FAIL_SAFE = "fail_safe"
    # 导演台专用
    CHAT = "chat"
    REJECT = "reject"
    GUIDE = "guide"
    PIPELINE = "pipeline"
    CLARIFY = "clarify"


class TerminalStatus(StrEnum):
    """显式终态：API 响应与审计统一携带，避免用自然语言判断结束方式。"""

    COMPLETED = "COMPLETED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    DENIED_BY_POLICY = "DENIED_BY_POLICY"
    FAILED_SAFE = "FAILED_SAFE"


class PolicyDecision(StrEnum):
    """策略引擎判定结果：确定性代码执行，不依赖模型置信度。"""

    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"


# --- 硬性预算（chat 单轮工具编排上限；超限安全降级而非失败） ---
MAX_TOOL_CALLS_PER_TURN = 12
MAX_TOOL_TOTAL_DURATION_MS = 30_000
MAX_CONTEXT_CHARS = 24_000


@dataclass
class ToolBudget:
    """单轮工具执行预算：调用前检查、调用后记账，超限拒绝后续调用。

    - 超限行为：跳过后续工具调用（安全降级），已有结果继续用于回答；
      违规记录进 violations 供审计，不静默丢弃
    - 上下文预算：注入 system_prompt 的工具上下文总字符数上限，
      防止工具返回过大撑爆 Token
    """

    max_calls: int = MAX_TOOL_CALLS_PER_TURN
    max_total_duration_ms: int = MAX_TOOL_TOTAL_DURATION_MS
    max_context_chars: int = MAX_CONTEXT_CHARS
    calls: int = 0
    total_duration_ms: int = 0
    context_chars: int = 0
    violations: list[str] = field(default_factory=list)

    def check_before(self, tool_name: str = "") -> str | None:
        """调用前检查预算；超限返回违规描述（含工具名），未超限返回 None。"""
        if self.calls >= self.max_calls:
            return f"{tool_name}:max_calls={self.max_calls}"
        if self.total_duration_ms >= self.max_total_duration_ms:
            return f"{tool_name}:max_total_duration_ms={self.max_total_duration_ms}"
        return None

    def record(self, duration_ms: int) -> None:
        """调用后记账（耗时为负时按 0 处理，防御异常计时）。"""
        self.calls += 1
        self.total_duration_ms += max(0, duration_ms)

    def context_allowed(self, chars: int) -> bool:
        """上下文预算检查：追加 chars 后是否仍在上限内。"""
        return self.context_chars + max(0, chars) <= self.max_context_chars

    def record_context(self, chars: int) -> None:
        self.context_chars += max(0, chars)

    def audit_payload(self) -> dict:
        """预算审计摘要：随 assistant 消息 payload 落库。"""
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "total_duration_ms": self.total_duration_ms,
            "context_chars": self.context_chars,
            "violations": list(self.violations),
        }


def evaluate_tool_policy(spec: dict, *, approved: bool = False) -> PolicyDecision:
    """工具策略引擎：基于注册表 spec 做确定性判定。

    - requires_approval 且未获批 → REQUIRE_CONFIRMATION（不执行、无副作用）
    - risk_level=high 且未获批 → REQUIRE_CONFIRMATION（高风险写操作必须人工审批）
    - 其余 → ALLOW

    置信度不参与判定：关键动作由确定性规则决定，不依赖模型输出。
    """
    if spec.get("requires_approval") and not approved:
        return PolicyDecision.REQUIRE_CONFIRMATION
    if spec.get("risk_level") == "high" and not approved:
        return PolicyDecision.REQUIRE_CONFIRMATION
    return PolicyDecision.ALLOW
