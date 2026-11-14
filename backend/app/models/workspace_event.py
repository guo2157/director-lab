"""工作台事件 outbox：导演工作台（导航/审批/任务/页面上下文）状态变更的事实记录。

复用 job_event / task_event 既有语义（不新建并行机制，符合禁重复建表约束）：
- event_seq 全局递增（自增主键），断线重连以 seq 严格大于判定
- 事件与业务写入同事务落库（Transactional Outbox），提交前不对外发布
- 事件只描述「发生了什么」，不承载 LLM 生成的可信度说明，禁止伪造进度

与 task_event 的边界：task_event 描述单个 Task 的执行阶段；
workspace_event 描述工作台层的跨对象事实（导航意图、审批流转、页面上下文变化），
两者可并存，前端分别消费，互不依赖页面生命周期。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base


class WorkspaceEvent(Base):
    """工作台事件：统一 outbox，供 /api/workspace/events SSE 增量推送。"""

    __tablename__ = "workspace_event"

    # 全局递增序列号（自增主键），客户端按 seq 严格大于判定增量
    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 事件类型，取值受 services/workspace_event.py 的 EVENT_TYPES 白名单约束
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    # 归属导演台会话（可空：系统级事件无会话上下文）
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 归属项目（可空：非项目级事件）
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 事件关联的目标路由名（导航类事件使用，便于前端按页过滤）
    route_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 结构化载荷：字段由各事件类型定义
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 事件产生时间（应用层显式赋值，不依赖数据库默认值）
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
