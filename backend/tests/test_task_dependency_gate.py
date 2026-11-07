"""依赖门禁与审批事件桥接测试（对话式任务执行改造 Phase 5）。
覆盖：
- 依赖门禁：depends_on 声明的前置阶段未 succeeded 时 start_stage 拒绝并写 task.stage_blocked 事件
- 前置 succeeded 后依赖阶段可启动
- 审批事件桥接：resolve_confirmation 写 task.approval_resolved 事件（approve/reject）
- 无关联统一 Task 时桥接安全返回 0
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Base, ids
from app.models.agent import AgentConfirmation, AgentTask
from app.models.task import Task, TaskEvent, TaskStage
from app.services import task_orchestrator as to


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _events(db, task_id: str, event_type: str) -> list:
    return (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id, TaskEvent.event_type == event_type)
        .all()
    )


# ---------- 依赖门禁 ----------


def test_stage_blocked_until_dependency_succeeded(db):
    task = to.create_task(db, session_id="s", task_type="generate", title="生成")
    dep = to.add_stage(db, task, stage_key="keyframe", stage_name="分镜图生成")
    follower = to.add_stage(
        db, task, stage_key="clip", stage_name="分镜视频生成", depends_on=["keyframe"],
    )
    to.start_task(db, task)

    # 前置未完成：依赖阶段启动被拒，写 task.stage_blocked 事件
    assert to.start_stage(db, task, follower) is False
    assert follower.status == "pending"
    assert len(_events(db, task.id, "task.stage_blocked")) == 1
    assert _events(db, task.id, "task.stage_blocked")[0].payload["blocked_by"] == "keyframe"

    # 前置完成后：依赖阶段可启动
    to.start_stage(db, task, dep)
    to.finish_stage(db, task, dep, "succeeded", result={"ok": True})
    assert to.start_stage(db, task, follower) is True
    assert follower.status == "running"


def test_stage_blocked_when_dependency_failed(db):
    task = to.create_task(db, session_id="s", task_type="generate", title="生成")
    dep = to.add_stage(db, task, stage_key="keyframe", stage_name="分镜图生成")
    follower = to.add_stage(
        db, task, stage_key="clip", stage_name="分镜视频生成", depends_on=["keyframe"],
    )
    to.start_task(db, task)
    to.start_stage(db, task, dep)
    to.finish_stage(db, task, dep, "failed", result={"error_code": "comfy_error"})

    # 前置 failed：依赖阶段仍被拦截
    assert to.start_stage(db, task, follower) is False
    assert follower.status == "pending"


def test_stage_blocked_when_dependency_missing(db):
    task = to.create_task(db, session_id="s", task_type="generate", title="生成")
    follower = to.add_stage(
        db, task, stage_key="clip", stage_name="分镜视频生成", depends_on=["nonexistent"],
    )
    to.start_task(db, task)
    # 依赖阶段不存在：拦截（不猜测执行）
    assert to.start_stage(db, task, follower) is False
    assert _events(db, task.id, "task.stage_blocked")[0].payload["dep_status"] == "missing"


def test_stage_without_dependency_starts_freely(db):
    task = to.create_task(db, session_id="s", task_type="generate", title="生成")
    stage = to.add_stage(db, task, stage_key="keyframe", stage_name="分镜图生成")
    to.start_task(db, task)
    # 无依赖声明：正常启动
    assert to.start_stage(db, task, stage) is True


# ---------- 审批事件桥接 ----------


def _mk_confirmation(db, session_id="ags_p5") -> tuple[AgentTask, AgentConfirmation]:
    now = datetime.now()
    agent_task = AgentTask(
        id=ids.agent_task_id(), session_id=session_id, message_id=None,
        intent="generate_request", intent_goal="生成", risk_level="medium",
        confidence=1.0, status="pending", created_at=now,
    )
    db.add(agent_task)
    db.flush()
    confirmation = AgentConfirmation(
        id=ids.agent_confirmation_id(), task_id=agent_task.id,
        tool_name="run_generate", args={"project_id": "prj_x"},
        status="pending", created_at=now,
    )
    db.add(confirmation)
    db.commit()
    return agent_task, confirmation


def test_approval_resolved_bridges_task_event(db):
    agent_task, confirmation = _mk_confirmation(db)
    # 统一 Task 关联该 AgentTask
    task = to.create_task(
        db, session_id=agent_task.session_id, task_type="generate",
        title="生成", agent_task_id=agent_task.id,
    )

    from app.services.approvals import resolve_confirmation
    resolved = resolve_confirmation(db, confirmation_id=confirmation.id, approve=True, comment="同意")
    assert resolved.status == "approved"
    # 审批裁决写 task.approval_resolved 事件
    events = _events(db, task.id, "task.approval_resolved")
    assert len(events) == 1
    assert events[0].payload["approve"] is True
    assert events[0].payload["confirmation_id"] == confirmation.id


def test_approval_rejected_bridges_task_event(db):
    agent_task, confirmation = _mk_confirmation(db)
    task = to.create_task(
        db, session_id=agent_task.session_id, task_type="generate",
        title="生成", agent_task_id=agent_task.id,
    )
    from app.services.approvals import resolve_confirmation
    resolve_confirmation(db, confirmation_id=confirmation.id, approve=False, comment="拒绝")
    events = _events(db, task.id, "task.approval_resolved")
    assert len(events) == 1
    assert events[0].payload["approve"] is False


def test_approval_bridge_without_unified_task_returns_zero(db):
    agent_task, confirmation = _mk_confirmation(db)
    # 不创建统一 Task：桥接安全返回 0
    assert to.bridge_approval_resolved(
        db, confirmation_id=confirmation.id, approve=True, comment=None,
    ) == 0
