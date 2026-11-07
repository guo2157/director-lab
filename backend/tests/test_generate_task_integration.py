"""生成任务接入统一 Task 层测试（对话式任务执行改造 Phase 4）。
覆盖：
- tool_run_generate 创建生成 Job 时同步创建统一 Task + generate 阶段（关联 job_id）
- Job 事件桥接自动闭环：job.finished(succeeded) → 阶段 succeeded → 任务 completed + 结果卡片
- Job 失败 → 阶段 failed → 任务 failed
- 幂等复用（同 request_key 复用 Job）时不重复建 Task
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker
from app.core.database import make_engine
from app.models import Asset, Base, Job, Project, Scene, Shot, Workflow, ids
from app.models.task import Task, TaskResult, TaskStage
from app.services import approvals
from app.services.agent_tools import execute_tool
from app.services import task_orchestrator as to


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'gen_task.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="生成任务项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
    shot = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    workflow = Workflow(
        id=ids.workflow_id(), name="默认图像工作流", workflow_type="image",
        is_default=True, is_builtin=True, version=1, definition={},
    )
    # D-23 资产门禁前置：场景绑定有效基准图（文件真实存在）+ 项目审核状态 approved
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    baseline_file = media / "baseline.png"
    baseline_file.write_bytes(b"x")
    baseline = Asset(
        id=ids.asset_id(), project_id=project.id, asset_type="scene",
        name="场景一基准图", version=1, file_path=str(baseline_file), validity="current",
    )
    scene.baseline_asset_id = baseline.id
    project.assets_review_status = "approved"
    # 状态机门禁前置：剧本版本 + 评估哈希一致 + parse 任务成功
    import hashlib
    from app.models import ScriptVersion
    script_text = "第一场：主角在河边。"
    script_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    script_version = ScriptVersion(
        id=ids.script_version_id(), project_id=project.id, version=1,
        content_snapshot=script_text, content_hash=script_hash, created_at=now,
    )
    parse_job = Job(
        id=ids.job_id(), project_id=project.id, job_type="parse",
        status="succeeded", created_at=now,
    )
    session.add_all([project, scene, shot, workflow, baseline, script_version, parse_job])
    session.flush()
    project.current_script_version_id = script_version.id
    project.estimated_text_hash = script_hash
    session.commit()
    yield session, project.id, shot.id
    session.close()


def _request_and_approve(session, pid, sid):
    requested = execute_tool(
        session, "request_generate", session_id="ags_p4", project_id=pid, shot_id=sid,
    )
    assert requested.ok, requested.error_message
    confirmation_id = requested.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True, comment="同意")
    return confirmation_id


def test_run_generate_creates_unified_task(db):
    session, pid, sid = db
    confirmation_id = _request_and_approve(session, pid, sid)
    result = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert result.ok, result.error_message
    job_id = result.data["job_id"]

    task = session.query(Task).filter(Task.project_id == pid).one()
    assert task.task_type == "generate"
    assert task.status == "running"
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.stage_key == "generate_keyframe"
    assert stage.stage_name == "分镜图生成"
    assert stage.job_id == job_id


def test_run_generate_idempotent_no_duplicate_task(db):
    session, pid, sid = db
    confirmation_id = _request_and_approve(session, pid, sid)
    first = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert first.ok
    second = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert second.ok
    assert second.data["reused"] is True
    # 幂等复用 Job：不重复建 Task
    assert session.query(Task).filter(Task.project_id == pid).count() == 1


def test_generate_bridge_auto_completes_with_result_card(db):
    session, pid, sid = db
    confirmation_id = _request_and_approve(session, pid, sid)
    result = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    job_id = result.data["job_id"]
    task = session.query(Task).filter(Task.project_id == pid).one()

    to.bridge_job_event(session, job_id=job_id, event_type="job.started", payload={"attempt": 1})
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.status == "running"

    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={"status": "succeeded", "result": {"assets": [{"asset_id": "ast_1"}], "prompt_id": "p1"}},
    )
    assert stage.status == "succeeded"
    assert task.status == "completed"
    assert task.progress == 100
    card = session.query(TaskResult).filter(TaskResult.task_id == task.id).one()
    assert card.data["assets"][0]["asset_id"] == "ast_1"


def test_generate_bridge_auto_fails(db):
    session, pid, sid = db
    confirmation_id = _request_and_approve(session, pid, sid)
    result = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    job_id = result.data["job_id"]
    task = session.query(Task).filter(Task.project_id == pid).one()

    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={"status": "failed", "error_code": "comfy_timeout", "error_msg": "等待超时"},
    )
    assert task.status == "failed"
    assert task.error_code == "comfy_timeout"
