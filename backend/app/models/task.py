"""统一任务编排层模型：对话式任务执行改造 Phase 2。

设计约束（Phase 1 审计结论）：
- Task 是编排层实体，Job 保持底层执行单元不变，不推倒重写执行层
- Task 通过 agent_task_id 关联 AgentTask（会话级意图任务），通过 TaskStage.job_id 关联 Job
- task_event 复用 job_event outbox 语义：全局递增 seq，断线重连以 seq 严格大于判定
- TaskResult 存结构化结果卡片数据，供前端任务卡片渲染
- LLM 不得编造状态：所有状态均由确定性代码写入，事件流是唯一事实来源
"""
from datetime import datetime
from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin
from app.models import ids


class Task(Base, TimestampMixin):
    """统一任务：一次用户目标在编排层的结构化执行单元。

    - 由 Task Orchestrator 创建：执行类意图（AgentTask）或显式流水线操作均可建 Task
    - status：pending / running / completed / failed / cancelled
    - progress：0-100 整数，由阶段进度聚合，不由 LLM 生成
    """

    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.task_id)
    # 归属会话（导演台会话，可空：系统触发的任务无会话上下文）
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 发起该任务的用户消息 ID：任务完成时据此把结果回推到同一条消息位置，
    # 页面刷新/会话切换后仍能定位到原始对话（不依赖内存上下文）
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 关联 AgentTask（意图任务，可空：非意图触发的任务）
    agent_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 关联项目（可空）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 任务类型：parse / generate / compose / export / custom
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    # 任务标题（展示用，来自用户目标或操作描述）
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # queued / pending / running / completed / failed / cancelled
    # queued：已受理待执行；pending 为历史取值，读侧按 queued 等价处理
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    # 聚合进度 0-100
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 当前执行步骤标识（如 reading_script / llm_analysis / normalize / saving_result）
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 当前步骤的展示文案（面向用户，由确定性代码写入，不由 LLM 生成）
    current_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 实际开始执行时间：耗时一律由该时间与当前时间计算，不依赖前端累加
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 失败时的错误码与错误信息
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_task_session_status", Task.__table__.c.session_id, Task.__table__.c.status)
Index("ix_task_project_type", Task.__table__.c.project_id, Task.__table__.c.task_type)


class TaskStage(Base, TimestampMixin):
    """任务阶段：Task 的执行阶段，一个阶段可派生一个 Job。

    - stage_index 任务内递增，驱动前端任务卡片按序展示
    - status：pending / running / succeeded / failed / skipped / cancelled
    - job_id 关联底层执行单元（Job），可空：纯回复类阶段无 Job
    """

    __tablename__ = "task_stage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.task_stage_id)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("task.id"), nullable=False, index=True)
    # 阶段序号（任务内递增）
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 阶段标识（如 parse / generate_characters / compose）
    stage_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 阶段名称（展示用）
    stage_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # pending / running / succeeded / failed / skipped / cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 阶段进度 0-100
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 关联底层 Job（可空）
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 阶段结果摘要
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 依赖门禁（Phase 5）：前置阶段 stage_key 列表，前置未全部 succeeded 时本阶段不可启动
    depends_on: Mapped[list | None] = mapped_column(JSON, nullable=True)


Index("ix_task_stage_task_idx", TaskStage.__table__.c.task_id, TaskStage.__table__.c.stage_index)


class TaskEvent(Base):
    """任务事件：统一事件流 outbox，复用 job_event 语义。

    - event_seq 全局递增（自增主键），断线重连以 seq 严格大于判定
    - 事件是任务状态的唯一事实来源，LLM 不得编造
    """

    __tablename__ = "task_event"

    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("task.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class TaskResult(Base, TimestampMixin):
    """任务结果：结构化结果卡片数据，供前端任务卡片渲染。

    - result_type：summary（摘要）/ assets（资产列表）/ shots（镜头列表）/ custom
    - data 为结构化 JSON，字段由各任务类型定义，前端按 result_type 渲染
    """

    __tablename__ = "task_result"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.task_result_id)
    task_id: Mapped[str] = mapped_column(String(32), ForeignKey("task.id"), nullable=False, index=True)
    result_type: Mapped[str] = mapped_column(String(32), nullable=False, default="summary")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
