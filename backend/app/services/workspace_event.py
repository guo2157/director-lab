"""工作台事件总线（Transactional Outbox）。

职责边界（导演工作台 V2 Phase 3）：
- 只负责「事实落库」与「增量读取」，不做任何业务决策，也不产生进度数值
- 写入必须与业务写入同一 Session/事务，由调用方 commit；本模块只 add + flush
- 事件类型受 EVENT_TYPES 白名单约束，未登记类型直接拒绝，防止事件语义漂移

与既有 outbox 的关系：
- job_event（app/models/production.py）/ task_event（app/models/task.py）语义一致：
  event_seq 全局递增、客户端按 seq 严格大于判定增量
- 本模块不改动上述两表，也不替代它们；task_event 继续描述 Task 执行阶段，
  workspace_event 描述工作台层跨对象事实（导航、审批、页面上下文）

禁止事项：
- 禁止在此伪造进度/耗时/成功率，payload 只能来自真实业务返回值
- 禁止为了「有事件」而补发事件；无事实即无事件
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.workspace_event import WorkspaceEvent

# 事件类型白名单：新增类型必须先在此登记，避免前后端约定漂移
EVENT_TYPES: frozenset[str] = frozenset(
    {
        # 导航：assistant 下发了 route_action，工作台已切换页面
        "workspace.navigate",
        # 页面上下文上报：前端告知当前所在页面与选中对象
        "workspace.page_context",
        # 审批：待确认项产生
        "workspace.approval_pending",
        # 审批：已处理（同意/拒绝）
        "workspace.approval_resolved",
        # 审批：已执行
        "workspace.approval_executed",
        # 任务：工作台侧观察到任务状态变化（只转发 task_event 的事实，不重复生成进度）
        "workspace.task_updated",
        # ── Agent Runtime（Phase B）：给前端看的事实流，只放 id/状态/错误码，不放自由文本 ──
        # payload: goal_id, status, lifecycle_state, description_len
        "agent.goal.created",
        # payload: goal_id, status, lifecycle_state
        "agent.goal.updated",
        # payload: goal_id, superseded_by
        "agent.goal.superseded",
        # payload: goal_id, status, lifecycle_state
        "agent.goal.finished",
        # payload: goal_id, plan_id, revision, steps[{step_id, step_key, tool_name, status}]
        "agent.plan.created",
        # payload: goal_id, plan_id, step_id, step_key, tool_name, status,
        #          job_id?, confirmation_id?, error_code?
        "agent.step.updated",
        # payload: goal_id, lifecycle_state, counts{succeeded, failed, waiting, pending}
        "agent.tick.finished",
    }
)

# 单次增量读取上限，避免一次拉取过多导致前端卡顿
_MAX_BATCH = 200


def write_workspace_event(
    db: Session,
    event_type: str,
    *,
    session_id: str | None = None,
    project_id: str | None = None,
    route_name: str | None = None,
    payload: dict | None = None,
) -> WorkspaceEvent:
    """写入一条工作台事件（不 commit，由调用方事务提交）。

    调用方必须保证：只有在业务写入成功的同一事务中调用，
    否则会出现「事件已发、数据未落」的假事件。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown workspace event_type: {event_type}")
    ev = WorkspaceEvent(
        event_type=event_type,
        session_id=session_id,
        project_id=project_id,
        route_name=route_name,
        payload=payload,
        created_at=datetime.now(),
    )
    db.add(ev)
    db.flush()
    return ev


def serialize_event(ev: WorkspaceEvent) -> dict:
    """事件对外序列化：字段与前端 useWorkspaceEvents 约定一致。"""
    data: dict = {
        "seq": ev.event_seq,
        "event_type": ev.event_type,
        "session_id": ev.session_id,
        "project_id": ev.project_id,
        "route_name": ev.route_name,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }
    if ev.payload:
        # payload 不得覆盖 seq/event_type 等信封字段
        for k, v in ev.payload.items():
            if k not in data:
                data[k] = v
    return data


def fetch_events_after(
    db: Session,
    last_seq: int,
    *,
    session_id: str | None = None,
    limit: int = _MAX_BATCH,
) -> list[WorkspaceEvent]:
    """按 seq 严格大于 last_seq 增量读取，升序返回。

    session_id 为可选过滤：不传则返回全局事件（多会话共享同一工作台时的默认行为）。
    """
    q = db.query(WorkspaceEvent).filter(WorkspaceEvent.event_seq > last_seq)
    if session_id:
        q = q.filter(WorkspaceEvent.session_id == session_id)
    return q.order_by(WorkspaceEvent.event_seq).limit(min(limit, _MAX_BATCH)).all()
