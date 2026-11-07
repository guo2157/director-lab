"""恢复对账与 LLM 状态纪律测试（对话式任务执行改造 Phase 7）。
覆盖：
- reconcile_orphan_tasks：孤儿 running Task（Job 已 succeeded 但桥接丢失）对账闭环 + 结果卡片
- 孤儿 Task（Job 已 failed）对账闭环失败
- Job 仍 queued/running 时保持 Task 现状（等待事件桥接）
- 无阶段/无 Job 关联的 Task 不处理
- LLM 状态纪律：系统提示词含任务状态纪律规则
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Base, Job, Project, ids
from app.models.task import Task, TaskResult, TaskStage
from app.services import task_orchestrator as to


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'reconcile.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="对账项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    session.commit()
    yield session, project
    session.close()


def _mk_job(db, project_id, status="succeeded", result=None, error_code=None):
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="parse",
        status=status, attempts=1, priority=0, submission_state="not_submitted",
        result=result, error_code=error_code, created_at=datetime.now(),
    )
    db.add(job)
    db.flush()
    return job


def _mk_orphan_task(db, project_id, job_id):
    """构造桥接丢失的孤儿 Task：Task running、阶段 running、Job 已终态。"""
    task = to.create_task(db, session_id="s", task_type="parse", title="解析", project_id=project_id)
    to.start_task(db, task)
    stage = to.add_stage(db, task, stage_key="parse", stage_name="剧本解析", job_id=job_id)
    stage.status = "running"  # 模拟桥接丢失：阶段停留在 running
    db.flush()
    return task, stage


def test_reconcile_closes_orphan_succeeded(db):
    session, project = db
    job = _mk_job(session, project.id, status="succeeded", result={"parse_version_id": "pv_9", "shots": 8})
    task, stage = _mk_orphan_task(session, project.id, job.id)

    closed = to.reconcile_orphan_tasks(session)
    assert closed == 1
    assert task.status == "completed"
    assert task.progress == 100
    assert stage.status == "succeeded"
    card = session.query(TaskResult).filter(TaskResult.task_id == task.id).one()
    assert card.data["shots"] == 8


def test_reconcile_closes_orphan_failed(db):
    session, project = db
    job = _mk_job(session, project.id, status="failed", error_code="llm_error")
    task, stage = _mk_orphan_task(session, project.id, job.id)

    closed = to.reconcile_orphan_tasks(session)
    assert closed == 1
    assert task.status == "failed"
    assert task.error_code == "llm_error"
    assert stage.status == "failed"


def test_reconcile_keeps_task_when_job_still_running(db):
    session, project = db
    job = _mk_job(session, project.id, status="queued")
    task, stage = _mk_orphan_task(session, project.id, job.id)

    # Job 仍 queued（恢复重置续跑）：Task 保持现状，等待事件桥接
    assert to.reconcile_orphan_tasks(session) == 0
    assert task.status == "running"
    assert stage.status == "running"


def test_reconcile_skips_task_without_job_stage(db):
    session, project = db
    # 无阶段 Task：不处理
    task = to.create_task(session, session_id="s", task_type="custom", title="无阶段")
    to.start_task(session, task)
    assert to.reconcile_orphan_tasks(session) == 0
    assert task.status == "running"

    # 阶段无 Job 关联：不处理
    task2 = to.create_task(session, session_id="s", task_type="custom", title="无Job阶段")
    to.start_task(session, task2)
    to.add_stage(session, task2, stage_key="reply", stage_name="纯回复")
    assert to.reconcile_orphan_tasks(session) == 0
    assert task2.status == "running"


def test_reconcile_idempotent_on_terminal_tasks(db):
    session, project = db
    job = _mk_job(session, project.id, status="succeeded", result={"shots": 1})
    task, stage = _mk_orphan_task(session, project.id, job.id)
    assert to.reconcile_orphan_tasks(session) == 1
    # 已闭环的 Task 不再处理（终态）
    assert to.reconcile_orphan_tasks(session) == 0


def test_system_prompt_contains_task_state_discipline():
    """LLM 状态纪律：系统提示词含任务状态纪律规则（不得编造状态）。"""
    from app.api.v1.agent import _DIRECTOR_SYSTEM_PROMPT
    assert "【任务状态纪律】" in _DIRECTOR_SYSTEM_PROMPT
    assert "不得编造" in _DIRECTOR_SYSTEM_PROMPT
    assert "task_card" in _DIRECTOR_SYSTEM_PROMPT
