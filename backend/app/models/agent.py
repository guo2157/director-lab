"""Agent 会话与消息模型：导演台对话持久化。

设计约束（AGENTS.md P0）：
- 会话与消息为独立于生产链路的对话域实体，不与 job/asset 状态机耦合
- 消息 role 取值：user / assistant / system / tool_event（工具调用事件）/ approval（人工审批事件）
- 消息 content 为 JSON 字段，兼容纯文本与结构化负载（工具调用参数、审批结果等）
- 会话关联 project_id 可空：导演台可在无项目上下文时进行通用问答
- 软删除：会话删除仅置 status=archived，不物理删除消息，保护审计记录
"""
from datetime import datetime
from sqlalchemy import JSON, DateTime, Integer, String, Text, func
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
    # 视频生成工作流状态（JSON）：stage（idle/check_project/project_created/ask_script_source/
    # wait_user_script/searching/generating/script_draft/script_confirmed）、project_name、
    # project_id、script_source（user/web）、script_draft、revision_count 等
    workflow_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 会话摘要（长期记忆）：历史消息超出短期窗口时，由 LLM 压缩为摘要保存，
    # 供后续对话与语义判定携带完整上下文
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 摘要已覆盖到的消息 seq（短期窗口从该 seq 之后开始）
    summary_upto_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 结构化上下文状态（JSON，Context Manager 持久化）：
    # focus（当前关注实体：entity_type/entity_id/entity_name）、
    # operation（最近操作：operation/target_entity/last_result/last_error）、
    # updated_seq（上下文更新到的消息 seq，用于过期判断）
    # 项目切换时由 Context Manager 清空 focus/operation，防止跨项目上下文污染
    context_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)


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

    # ── Agent Runtime M1 扩展列 ──
    goal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_step_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifecycle_state: Mapped[str | None] = mapped_column(String(32), nullable=True, default="created", server_default="created")
    progress_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resume_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentPlanStep(Base, TimestampMixin):
    """Agent 计划步骤：任务的执行计划（B 档：模型提议、代码裁决）。

    - step_type：tool_call（工具调用）/ clarification（向用户澄清）/ reply（生成回复）
    - 工具调用步骤须引用 TOOL_REGISTRY 中已注册工具名
    - status：pending / running / succeeded / failed / skipped
    """

    __tablename__ = "agent_plan_step"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 外键指向 agent_task.id（前缀 agt_），而非统一 task.id（前缀 task_）。
    # V2 修正注记：不能直接 db.get(Task, plan_step.task_id)——ID 前缀不同会静默返回 None。
    # 跨表查询应通过 agent_task_id 反查 Task：select(Task).where(Task.agent_task_id == plan_step.task_id)。
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
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    # ── Agent Runtime M1 扩展列 ──
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)
    preconditions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requires_approval: Mapped[bool | None] = mapped_column(nullable=True, default=False, server_default="0")
    retry_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0, server_default="0")
    max_retries: Mapped[int | None] = mapped_column(Integer, nullable=True, default=3, server_default="3")
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, default=600, server_default="600")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentConfirmation(Base, TimestampMixin):
    """确认事项记录：统一承载两类人工确认（B 档）。

    - kind=tool_approval：高风险工具审批门禁，requires_approval=True 的工具执行前
      须有 approved 记录；task_id 指向承载意图快照的 AgentTask
    - kind=artifact_review：业务对象人工审核（如剧本第 N 版），由待确认事项管理器
      （services/review_confirmation.py）创建与裁决；审核结论绑定明确对象与版本，
      用户拒绝后由后端阻断依赖任务

    status 取值：pending / approved / rejected / revision_requested /
    deferred / expired / cancelled
    审核事件同步以 AgentMessage(role="approval") 落审计消息，可追溯且不进入对话上下文。

    持久化要求：确认事项创建即落库；页面刷新、Agent 重启、会话恢复后都必须能重读，
    不允许只存在于聊天上下文（业务状态不得由 LLM 单独决定）。
    """

    __tablename__ = "agent_confirmation"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # tool_approval（工具审批）/ artifact_review（业务对象审核）
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tool_approval",
        server_default="tool_approval", index=True,
    )
    # 承载意图快照的 AgentTask；artifact_review 时为空
    task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 待审批工具名（TOOL_REGISTRY）；artifact_review 时为空字符串
    tool_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    # 审批时的入参快照（JSON）
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending / approved / rejected / revision_requested / deferred / expired / cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # 审批意见 / 修改要求
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 业务对象审核字段（kind=artifact_review）---
    # 所属会话与项目：可直接查询，页面刷新后无需经 task 反查即可重建确认卡片
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 审核对象类型：script / assets / keyframes / clips 等
    target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 审核对象 ID（剧本审核为 project_id：剧本归属项目且单版本覆盖）
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 审核对象版本（剧本审核为 ScriptVersion.version）；审核结论只对该版本生效
    target_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 审核轮次：同一对象同一版本同一轮次只允许一个有效待确认事项
    round: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    # 审核类型（script_review / assets_review / keyframes_review / clips_review）
    confirmation_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Agent 提出的确认问题原文（刷新后据此重建卡片与上下文，不必靠 LLM 猜测）
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 允许的答复类型（JSON 数组：approve / reject / revise / defer）
    allowed_responses: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 用户原始答复原文（留证据，便于审计与复现）
    user_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 解析结论（JSON：resolution=approve/reject/revise/defer、matched_by、note）
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 幂等键：会话+项目+对象+版本+轮次+类型；重复消息不再新建确认事项
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AgentMemory(Base, TimestampMixin):
    """Agent 记忆：跨会话持久化的项目记忆、用户偏好与失败经验（记忆系统迭代一）。
    设计约束（AGENTS.md P0）：
    - 记忆只作上下文补充，不得覆盖用户当前明确指令
    - 记忆必须可追溯：source 记录来源（user 明确要求 / agent 提炼），
      source_ref 保留消息或任务引用
    - 记忆有生命周期：status=superseded 表示被更新替代，保留记录可追溯，不物理删除
    """
    __tablename__ = "agent_memory"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # project（项目生产规范与设定）/ preference（用户长期偏好）/ experience（失败经验规则）
    memory_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="project", index=True
    )
    # 关联项目（preference 全局偏好时为空）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 记忆正文（简洁条目式，单条不超过 500 字）
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 来源：user（用户明确要求记住）/ agent（系统从对话或任务提炼）
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    # 来源引用（JSON：message_id / task_id 等，保证可追溯）
    source_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # active / superseded
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True
    )
    # 替代本条记忆的新记忆 ID（status=superseded 时填写）
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AgentGoal(Base, TimestampMixin):
    """Agent 用户目标：一次用户意图的结构化持久化（Agent Runtime Phase 1）。
    设计约束：
    - 每个 session 同一时间只有一个 active 目标
    - 目标类型：user_request / system_continuation / recovery
    - 约束与成功标准以 JSON 存储，供 Constraint Manager 与 Verifier 消费
    - 状态机：active → paused/completed/failed/cancelled；failed 可恢复为 active
    """

    __tablename__ = "agent_goal"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    goal_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user_request", server_default="user_request"
    )
    constraints: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    success_criteria: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active", index=True
    )


class AgentPlan(Base, TimestampMixin):
    """Agent 执行计划：每个 Goal 可有多个 Revision（Agent Runtime Phase 1）。
    设计约束：
    - 计划版本号 revision 递增，旧版本 status=superseded 保留审计
    - plan_json 存储完整计划快照（含 steps 列表）
    - 状态机：active → superseded/completed/failed
    """

    __tablename__ = "agent_plan"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )


class AgentExecutionLog(Base, TimestampMixin):
    """Agent 执行审计日志：每次工具调用/Step 执行的完整记录（Agent Runtime Phase 1）。
    设计约束：
    - 记录 tool_name、input、output 完整快照
    - 记录耗时 duration_ms 供性能分析
    - 失败时记录 error_code 与 error_message，不丢失现场
    - 关联 goal_id/plan_id/step_id 三级，支持逐层追溯
    """

    __tablename__ = "agent_execution_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    goal_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    step_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
