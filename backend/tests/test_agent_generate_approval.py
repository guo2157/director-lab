"""生成类工具审批门禁回归测试。

覆盖：
- 申请阶段只落审批记录，不创建任何生成任务（无副作用）
- 未获批执行被拦下，任务数仍为 0（审批账实相符）
- 获批后执行创建任务，重复执行复用同一任务（幂等，不重复扣成本）
- 同会话同入参重复申请复用同一条待审记录
- 重复裁决被拒绝
- 镜头不属于指定项目时拒绝，防跨项目触发生成
"""
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, Scene, Shot, Workflow, ids
from app.services import approvals
from app.services.agent_tools import TOOL_REGISTRY, execute_tool


@pytest.fixture()
def db(tmp_path):
    """内存库 + 一个项目、一个场景、一个镜头、一个默认图像工作流。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'gen.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(),
        name="审批测试项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
    )
    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
    shot = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    workflow = Workflow(
        id=ids.workflow_id(),
        name="默认图像工作流",
        workflow_type="image",
        is_default=True,
        is_builtin=True,
        version=1,
        definition={},
    )
    session.add_all([project, scene, shot, workflow])
    session.commit()
    yield session, project.id, shot.id
    session.close()


def _job_count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(Job)) or 0)


def test_generate_tools_registered_with_approval_gate():
    """两个生成工具均已注册，执行工具标记为需审批。"""
    assert {"request_generate", "run_generate"} <= set(TOOL_REGISTRY)
    assert TOOL_REGISTRY["request_generate"]["permission"] == "generate"
    assert TOOL_REGISTRY["request_generate"]["requires_approval"] is False
    assert TOOL_REGISTRY["run_generate"]["requires_approval"] is True
    assert TOOL_REGISTRY["run_generate"]["side_effect"] is True


def test_request_creates_approval_only(db):
    """申请只创建审批记录，不产生任何生成任务。"""
    session, pid, sid = db
    result = execute_tool(
        session, "request_generate", session_id="ags_t1", project_id=pid, shot_id=sid,
        target="keyframe",
    )
    assert result.ok, result.error_message
    assert result.data["status"] == "pending"
    assert _job_count(session) == 0


def test_run_blocked_without_approval(db):
    """未获批时执行被拦下，且不产生任务（账实相符）。"""
    session, pid, sid = db
    requested = execute_tool(
        session, "request_generate", session_id="ags_t2", project_id=pid, shot_id=sid
    )
    confirmation_id = requested.data["confirmation_id"]

    blocked = execute_tool(session, "run_generate", confirmation_id=confirmation_id)
    assert not blocked.ok
    assert blocked.error_code == "approval_required"
    assert _job_count(session) == 0


def test_run_after_approval_creates_single_job(db):
    """获批后执行创建一条任务，重复执行复用同一条（幂等，不重复扣成本）。"""
    session, pid, sid = db
    requested = execute_tool(
        session, "request_generate", session_id="ags_t3", project_id=pid, shot_id=sid
    )
    confirmation_id = requested.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True, comment="同意")

    first = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert first.ok, first.error_message
    assert first.data["reused"] is False
    assert _job_count(session) == 1

    second = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert second.ok
    assert second.data["reused"] is True
    assert second.data["job_id"] == first.data["job_id"]
    assert _job_count(session) == 1


def test_request_idempotent_for_same_args(db):
    """同会话同入参重复申请复用同一条待审记录。"""
    session, pid, sid = db
    first = execute_tool(
        session, "request_generate", session_id="ags_t4", project_id=pid, shot_id=sid
    )
    second = execute_tool(
        session, "request_generate", session_id="ags_t4", project_id=pid, shot_id=sid
    )
    assert first.data["confirmation_id"] == second.data["confirmation_id"]
    assert len(approvals.list_pending(session, "ags_t4")) == 1


def test_repeat_resolve_rejected(db):
    """已裁决的审批不能重复裁决。"""
    session, pid, sid = db
    requested = execute_tool(
        session, "request_generate", session_id="ags_t5", project_id=pid, shot_id=sid
    )
    confirmation_id = requested.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True)

    with pytest.raises(Exception) as exc:
        approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True)
    assert getattr(exc.value, "status_code", None) == 409 or "confirmation_already_resolved" in str(
        exc.value
    )


def test_shot_must_belong_to_project(db):
    """镜头不属于指定项目时拒绝，避免跨项目触发生成。"""
    session, _pid, sid = db
    now = datetime.now()
    other = Project(
        id=ids.project_id(),
        name="另一个项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(other)
    session.commit()

    result = execute_tool(
        session, "request_generate", session_id="ags_t6", project_id=other.id, shot_id=sid
    )
    assert not result.ok
    assert result.error_code == "shot_not_in_project"
    assert _job_count(session) == 0
