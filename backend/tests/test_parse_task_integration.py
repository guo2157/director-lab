"""剧本解析任务接入统一 Task 层测试（对话式任务执行改造 Phase 3）。
覆盖：
- tool_analyze_script 创建解析 Job 时同步创建统一 Task + parse 阶段（关联 job_id）
- Job 事件桥接自动闭环：job.finished(succeeded) → 阶段 succeeded → 任务 completed + 结果卡片
- Job 失败 → 阶段 failed → 任务 failed
- update_stage_progress_by_job 按 job_id 更新阶段进度
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Base, Job, Project, ScriptVersion, ids
from app.models.task import Task, TaskResult, TaskStage
from app.services import task_orchestrator as to


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'parse_task.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="解析项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot="第一集剧本内容", content_hash="hash1",
        created_at=now,
    )
    session.add(sv)
    session.commit()
    yield session, project, sv
    session.close()


def test_analyze_script_creates_unified_task(db):
    session, project, sv = db
    from app.services.pipeline_tools import tool_analyze_script
    result = tool_analyze_script(session, project.id, episode_start=1, episode_count=1)
    job_id = result["job_id"]
    # 统一 Task 与阶段已创建并关联
    task = session.query(Task).filter(Task.project_id == project.id).one()
    assert task.task_type == "parse"
    assert task.status == "running"
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.stage_key == "parse"
    assert stage.job_id == job_id


def test_bridge_auto_completes_task_with_result_card(db):
    session, project, sv = db
    from app.services.pipeline_tools import tool_analyze_script
    result = tool_analyze_script(session, project.id, episode_start=1, episode_count=1)
    job_id = result["job_id"]
    task = session.query(Task).filter(Task.project_id == project.id).one()

    # 模拟执行器事件序列：job.started → job.finished(succeeded)
    to.bridge_job_event(session, job_id=job_id, event_type="job.started", payload={"attempt": 1})
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.status == "running"
    assert task.status == "running"

    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={"status": "succeeded", "result": {"parse_version_id": "pv_1", "shots": 12}},
    )
    # 自动闭环：阶段 succeeded → 任务 completed + 结果卡片
    assert stage.status == "succeeded"
    assert task.status == "completed"
    assert task.progress == 100
    card = session.query(TaskResult).filter(TaskResult.task_id == task.id).one()
    assert card.result_type == "shots"
    assert card.data["shots"] == 12
    assert card.data["parse_version_id"] == "pv_1"


def test_bridge_auto_fails_task(db):
    session, project, sv = db
    from app.services.pipeline_tools import tool_analyze_script
    result = tool_analyze_script(session, project.id, episode_start=1, episode_count=1)
    job_id = result["job_id"]
    task = session.query(Task).filter(Task.project_id == project.id).one()

    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={"status": "failed", "error_code": "llm_error", "error_msg": "模型超时"},
    )
    assert task.status == "failed"
    assert task.error_code == "llm_error"


def test_update_stage_progress_by_job(db):
    session, project, sv = db
    from app.services.pipeline_tools import tool_analyze_script
    result = tool_analyze_script(session, project.id, episode_start=1, episode_count=1)
    job_id = result["job_id"]
    task = session.query(Task).filter(Task.project_id == project.id).one()

    n = to.update_stage_progress_by_job(session, job_id, 50)
    assert n == 1
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.progress == 50
    assert task.progress == 50
    # 无关联 Job 时返回 0
    assert to.update_stage_progress_by_job(session, "job_nonexistent", 50) == 0
