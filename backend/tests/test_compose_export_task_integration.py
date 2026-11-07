"""合成/导出任务接入统一 Task 层测试（对话式任务执行改造 Phase 6）。
覆盖：
- tool_compose_video 创建合成 Job 时同步创建统一 Task + compose 阶段（关联 job_id）
- tool_export_video 创建导出 Job 时同步创建统一 Task + export 阶段（关联 job_id）
- Job 事件桥接自动闭环（复用 Phase 3 机制）
说明：状态机门禁（compose 阶段前置）属 pipeline_state 既有测试范围，此处 monkeypatch 绕过以聚焦编排层逻辑。
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Base, Composition, Project, ids
from app.models.task import Task, TaskResult, TaskStage
from app.services import task_orchestrator as to


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'compose_task.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="合成导出项目", aspect_ratio="16:9",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    session.commit()
    # 绕过状态机门禁（compose 阶段前置属 pipeline_state 既有测试范围）
    import app.services.pipeline_tools as pt
    monkeypatch.setattr(pt, "assert_action_allowed", lambda db, pid, action: None)
    yield session, project
    session.close()


def test_compose_video_creates_unified_task(db):
    session, project = db
    from app.services.pipeline_tools import tool_compose_video
    result = tool_compose_video(session, project.id, episode=1)
    job_id = result["job_id"]

    task = session.query(Task).filter(Task.project_id == project.id, Task.task_type == "compose").one()
    assert task.status == "running"
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.stage_key == "compose"
    assert stage.stage_name == "成片合成"
    assert stage.job_id == job_id


def test_compose_bridge_auto_completes(db):
    session, project = db
    from app.services.pipeline_tools import tool_compose_video
    result = tool_compose_video(session, project.id, episode=1)
    job_id = result["job_id"]
    task = session.query(Task).filter(Task.project_id == project.id, Task.task_type == "compose").one()

    to.bridge_job_event(session, job_id=job_id, event_type="job.started", payload={"attempt": 1})
    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={"status": "succeeded", "result": {"composition_id": "cmp_1", "duration": 120}},
    )
    assert task.status == "completed"
    assert task.progress == 100
    card = session.query(TaskResult).filter(TaskResult.task_id == task.id).one()
    assert card.data["composition_id"] == "cmp_1"


def test_export_video_creates_unified_task(db):
    session, project = db
    # 前置：成功的合成版本
    comp = Composition(
        id=ids.new_id("cmp"), project_id=project.id, status="done",
        stale=False, created_at=datetime.now(),
    )
    session.add(comp)
    session.commit()

    from app.services.pipeline_tools import tool_export_video
    result = tool_export_video(session, project.id, composition_id=comp.id)
    job_id = result["job_id"]

    task = session.query(Task).filter(Task.project_id == project.id, Task.task_type == "export").one()
    assert task.status == "running"
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.stage_key == "export"
    assert stage.stage_name == "成片导出"
    assert stage.job_id == job_id


def test_export_bridge_auto_completes(db):
    session, project = db
    comp = Composition(
        id=ids.new_id("cmp"), project_id=project.id, status="done",
        stale=False, created_at=datetime.now(),
    )
    session.add(comp)
    session.commit()

    from app.services.pipeline_tools import tool_export_video
    result = tool_export_video(session, project.id, composition_id=comp.id)
    job_id = result["job_id"]
    task = session.query(Task).filter(Task.project_id == project.id, Task.task_type == "export").one()

    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={"status": "succeeded", "result": {"export_id": "exp_1", "file_name": "out.mp4"}},
    )
    assert task.status == "completed"
    card = session.query(TaskResult).filter(TaskResult.task_id == task.id).one()
    assert card.data["export_id"] == "exp_1"
