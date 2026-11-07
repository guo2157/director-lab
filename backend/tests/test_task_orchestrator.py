"""Task Orchestrator 单元测试（统一对话式任务执行改造 Phase 2）。
覆盖：
- Task 生命周期：created → started → finished/failed/cancelled 事件写入 outbox
- 幂等：同一 agent_task_id 重复创建返回已有 Task
- 阶段管理：add/start/progress/finish，stage_index 递增，进度聚合
- 状态机门禁：终态不可再变更，非法流转拒绝
- Job 事件桥接：job.started/job.finished → task.stage_started/stage_succeeded
- task_payload 卡片数据结构
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Base, Job, ids
from app.models.task import Task, TaskEvent, TaskResult, TaskStage
from app.services import task_orchestrator as to


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'task.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _mk_job(db, job_type="parse") -> Job:
    from app.models import Project
    project = Project(
        id=ids.project_id(), name="测试项目", aspect_ratio="9:16",
        creation_type="script", status="active",
        created_at=datetime.now(), updated_at=datetime.now(),
    )
    db.add(project)
    db.flush()
    job = Job(
        id=ids.job_id(),
        project_id=project.id,
        job_type=job_type,
        status="queued",
        attempts=0,
        priority=0,
        submission_state="not_submitted",
        created_at=datetime.now(),
    )
    db.add(job)
    db.commit()
    return job


def _events(db, task_id: str) -> list[str]:
    rows = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.event_seq)
        .all()
    )
    return [r.event_type for r in rows]


# ---------- 生命周期 ----------


def test_task_lifecycle_events(db):
    t = to.create_task(db, session_id="ags_x", task_type="parse", title="解析剧本")
    # 新任务统一以 queued 受理（历史 pending 为其等价旧值），并带初始步骤文案
    assert t.status == "queued"
    assert t.current_step == "queued"
    assert t.current_message
    assert _events(db, t.id) == ["task.created"]

    assert to.start_task(db, t) is True
    assert t.status == "running"
    assert "task.started" in _events(db, t.id)

    assert to.complete_task(db, t, summary="完成", result_data={"result_type": "summary", "shots": 10}) is True
    assert t.status == "completed"
    assert t.progress == 100
    assert "task.finished" in _events(db, t.id)
    # 结果卡片落库
    r = db.query(TaskResult).filter(TaskResult.task_id == t.id).one()
    assert r.data["shots"] == 10


def test_task_fail_and_cancel(db):
    t = to.create_task(db, session_id=None, task_type="generate", title="生成")
    assert to.fail_task(db, t, error_code="comfy_error", error_msg="超时") is True
    assert t.status == "failed"
    assert t.error_code == "comfy_error"
    # 终态不可再变更
    assert to.complete_task(db, t, summary="x") is False
    assert to.cancel_task(db, t) is False
    assert t.status == "failed"

    t2 = to.create_task(db, session_id=None, task_type="generate", title="生成2")
    assert to.cancel_task(db, t2, reason="用户取消") is True
    assert t2.status == "cancelled"


def test_create_task_idempotent_by_agent_task_id(db):
    t1 = to.create_task(db, session_id="s", task_type="parse", title="A", agent_task_id="agt_1")
    t2 = to.create_task(db, session_id="s", task_type="parse", title="A", agent_task_id="agt_1")
    assert t1.id == t2.id
    assert db.query(Task).count() == 1
    # 无 agent_task_id 时不做幂等
    t3 = to.create_task(db, session_id="s", task_type="parse", title="B")
    assert t3.id != t1.id


# ---------- 阶段与进度 ----------


def test_stage_lifecycle_and_progress_aggregation(db):
    t = to.create_task(db, session_id="s", task_type="parse", title="解析")
    s1 = to.add_stage(db, t, stage_key="parse", stage_name="剧本解析")
    s2 = to.add_stage(db, t, stage_key="review", stage_name="资产审核")
    assert s1.stage_index == 0 and s2.stage_index == 1
    assert "task.stage_added" in _events(db, t.id)

    to.start_stage(db, t, s1)
    assert s1.status == "running"
    # 任一阶段 running → 任务 running
    assert t.status == "running"

    to.update_stage_progress(db, t, s1, 60)
    assert s1.progress == 60
    # 聚合进度 = (60 + 0) / 2
    assert t.progress == 30
    assert "task.stage_progress" in _events(db, t.id)

    assert to.finish_stage(db, t, s1, "succeeded", result={"shots": 5}) is True
    assert s1.progress == 100
    # 聚合进度 = (100 + 0) / 2
    assert t.progress == 50
    assert "task.stage_succeeded" in _events(db, t.id)

    # 阶段终态门禁：succeeded 不可再流转
    assert to.finish_stage(db, t, s1, "failed") is False


def test_stage_failed_retry_back_to_running(db):
    t = to.create_task(db, session_id="s", task_type="generate", title="生成")
    s1 = to.add_stage(db, t, stage_key="gen", stage_name="生成")
    to.start_stage(db, t, s1)
    assert to.finish_stage(db, t, s1, "failed") is True
    # failed 可重试回 running
    assert to.start_stage(db, t, s1) is True
    assert s1.status == "running"


# ---------- Job 事件桥接 ----------


def test_bridge_job_event_started_finished(db):
    job = _mk_job(db)
    t = to.create_task(db, session_id="s", task_type="parse", title="解析")
    stage = to.add_stage(db, t, stage_key="parse", stage_name="剧本解析", job_id=job.id)

    # job.started → 阶段 running
    n = to.bridge_job_event(db, job_id=job.id, event_type="job.started", payload={"attempt": 1})
    assert n == 1
    assert stage.status == "running"
    assert t.status == "running"

    # job.finished + status=succeeded → 阶段 succeeded
    n = to.bridge_job_event(
        db, job_id=job.id, event_type="job.finished",
        payload={"status": "succeeded", "result": {"shots": 8}},
    )
    assert n == 1
    assert stage.status == "succeeded"
    assert stage.progress == 100
    assert t.progress == 100
    assert "task.stage_succeeded" in _events(db, t.id)


def test_bridge_job_event_failed(db):
    job = _mk_job(db)
    t = to.create_task(db, session_id="s", task_type="generate", title="生成")
    stage = to.add_stage(db, t, stage_key="gen", stage_name="生成", job_id=job.id)
    to.start_stage(db, t, stage)

    n = to.bridge_job_event(
        db, job_id=job.id, event_type="job.finished",
        payload={"status": "failed", "error_code": "comfy_timeout"},
    )
    assert n == 1
    assert stage.status == "failed"
    assert "task.stage_failed" in _events(db, t.id)


def test_bridge_job_event_no_task_returns_zero(db):
    job = _mk_job(db)
    # Job 不属于任何任务：桥接返回 0，不报错
    assert to.bridge_job_event(db, job_id=job.id, event_type="job.started", payload={}) == 0


def test_bridge_job_event_retry_passthrough(db):
    job = _mk_job(db)
    t = to.create_task(db, session_id="s", task_type="generate", title="生成")
    to.add_stage(db, t, stage_key="gen", stage_name="生成", job_id=job.id)
    n = to.bridge_job_event(db, job_id=job.id, event_type="job.retry", payload={"attempt": 2})
    assert n == 1
    assert "task.stage_progress" in _events(db, t.id)


# ---------- 任务卡片数据 ----------


def test_task_payload_structure(db):
    t = to.create_task(db, session_id="s", task_type="parse", title="解析剧本")
    s1 = to.add_stage(db, t, stage_key="parse", stage_name="剧本解析")
    to.start_stage(db, t, s1)
    to.update_stage_progress(db, t, s1, 50)
    to.complete_task(db, t, summary="完成", result_data={"result_type": "summary", "ok": True})

    card = to.task_payload(db, t)
    assert card["task_id"] == t.id
    assert card["status"] == "completed"
    assert card["progress"] == 100
    assert len(card["stages"]) == 1
    assert card["stages"][0]["stage_key"] == "parse"
    assert card["result"]["ok"] is True
