"""Agent 会话与消息模型：导演台对话持久化。

设计约束（AGENTS.md P0）：
- 会话与消息为独立于生产链路的对话域实体，不与 job/asset 状态机耦合
- 消息 role 取值：user / assistant / system / tool_event（工具调用事件）/ approval（人工审批事件）
- 消息 content 为 JSON 字段，兼容纯文本与结构化负载（工具调用参数、审批结果等）
- 会话关联 project_id 可空：导演台可在无项目上下文时进行通用问答
- 软删除：会话删除仅置 status=archived，不物理删除消息，保护审计记录
"""
from datetime import datetime
from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class AgentSession(Base, TimestampMixin):
    """导演台会话：一次连续的多轮对话上下文。"""

    __tablename__ = "agent_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="新会话")
    # 关联项目（可空：通用问答无项目上下文）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # active / archived
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    # 会话使用的语言模型（快照，切换模型不回写历史消息）
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AgentMessage(Base, TimestampMixin):
    """导演台消息：会话内的一条用户/助手/工具/审批事件记录。"""

    __tablename__ = "agent_message"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # user / assistant / system / tool_event / approval
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # 纯文本内容（user/assistant 消息正文）
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 结构化负载：工具调用（tool_name/args/result）、审批（target/action/comment）、附件引用列表
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 关联附件（upload_record id 列表，存 payload 中；此字段冗余首个附件便于检索）
    attachment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 消息序号（会话内递增，用于排序与增量加载）
    seq: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentTask(Base, TimestampMixin):
    """Agent 任务：一次用户目标的结构化执行单元（B 档：意图识别落库）。

    - 由结构化意图识别结果创建：goal/risk_level/success_criteria 来自 IntentResult
    - status：pending（待执行）/ running / completed / failed / cancelled
    - 工具调用仍由确定性代码执行，模型只负责选择与解释（AGENTS.md P0）
    """

    __tablename__ = "agent_task"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 触发消息（user 消息 id）
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 结构化意图识别结果快照
    intent: Mapped[str] = mapped_column(String(32), nullable=False, default="query")
    intent_goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False, default="low")
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)
    # 可验证的任务完成标准（JSON 数组）
    success_criteria: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending / running / completed / failed / cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # 执行结果摘要（completed/failed 时填写）
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentPlanStep(Base, TimestampMixin):
    """Agent 计划步骤：任务的执行计划（B 档：模型提议、代码裁决）。

    - step_type：tool_call（工具调用）/ clarification（向用户澄清）/ reply（生成回复）
    - 工具调用步骤须引用 TOOL_REGISTRY 中已注册工具名
    - status：pending / running / succeeded / failed / skipped
    """

    __tablename__ = "agent_plan_step"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 步骤序号（任务内递增）
    step_index: Mapped[int] = mapped_column(nullable=False, default=0)
    # tool_call / clarification / reply
    step_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # tool_call 时为 TOOL_REGISTRY 工具名
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 步骤入参（JSON，经 input_model 校验后落库）
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending / running / succeeded / failed / skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 执行结果摘要（错误码/耗时等）
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AgentConfirmation(Base, TimestampMixin):
    """Agent 审批记录：高风险操作的审批门禁（B 档）。

    - requires_approval=True 的工具执行前须有 approved 记录
    - status：pending（待审批）/ approved / rejected
    - 审批事件同步以 AgentMessage(role="approval") 落审计消息
    """

    __tablename__ = "agent_confirmation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 待审批工具名（TOOL_REGISTRY）
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 审批时的入参快照（JSON）
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending / approved / rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # 审批意见（approved/rejected 时填写）
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
