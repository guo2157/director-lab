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
from app.models import Asset, Base, Job, ParseVersion, Project, Scene, Shot, Workflow, ids
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
    parse_version = ParseVersion(
        id=ids.parse_version_id(), project_id=project.id, script_version_id=script_version.id,
        version=1, status="current", script_hash=script_hash, created_at=now,
    )
    session.add_all([project, scene, shot, workflow, baseline, script_version, parse_job, parse_version])
    session.flush()
    project.current_script_version_id = script_version.id
    project.current_parse_version_id = parse_version.id
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


def test_generate_bridge_writes_result_message_to_session(db):
    """生成完成后把产出图片回写成会话消息：导演台气泡据此展示结果图。"""
    session, pid, sid = db
    from app.models import AgentSession
    from app.models.agent import AgentMessage

    session.add(AgentSession(id="ags_p4", title="生成对话", status="active"))
    session.commit()

    confirmation_id = _request_and_approve(session, pid, sid)
    result = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    job_id = result.data["job_id"]
    task = session.query(Task).filter(Task.project_id == pid).one()

    to.bridge_job_event(
        session, job_id=job_id, event_type="job.finished",
        payload={
            "status": "succeeded",
            "result": {
                "assets": [{
                    "asset_id": "ast_1", "asset_type": "keyframe", "out_name": "shot_1.png",
                }],
                "prompt_id": "p1",
            },
        },
    )
    assert task.status == "completed"
    msg = (
        session.query(AgentMessage)
        .filter(AgentMessage.session_id == "ags_p4", AgentMessage.role == "assistant")
        .one()
    )
    assert msg.payload["task_id"] == task.id
    ref = msg.payload["asset_refs"][0]
    assert ref["asset_id"] == "ast_1"
    assert ref["has_thumb"] is True
    assert "生成完成" in msg.content


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


# ── 对话内随手生成（文生图 / 图生图）：气泡内真实出图 ──


@pytest.fixture()
def quick_db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'quick_gen.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    t2i = Workflow(
        id=ids.workflow_id(), name="文生图", workflow_type="image",
        version=1, definition={}, status="published", is_builtin=True,
        capabilities={"generation_modes": ["text_to_image"], "reference_slots": []},
    )
    i2i = Workflow(
        id=ids.workflow_id(), name="图生图", workflow_type="image",
        version=1, definition={}, status="published", is_builtin=True,
        capabilities={
            "generation_modes": ["image_to_image", "multi_reference"],
            "reference_slots": [{"param": "ref_scene"}, {"param": "ref_identity"}],
        },
    )
    session.add_all([t2i, i2i])
    session.commit()
    yield session, tmp_path
    session.close()


def test_quick_generate_text_to_image_creates_job(quick_db):
    from app.api.v1 import agent as agent_api

    session, _ = quick_db
    result = agent_api._try_quick_generate(session, "ags_quick", "生成一张小狗的图片", [])
    assert result is not None
    session.commit()

    job = session.get(Job, result["job_id"])
    assert job.status == "queued"
    assert job.shot_id is None
    assert job.params["__quick_generate__"] is True
    assert job.params["prompt"] == "生成一张小狗的图片"
    project = session.get(Project, job.project_id)
    assert project.name == "随手生成"

    task = session.get(Task, result["task"].id)
    assert task.task_type == "generate"
    assert task.project_id == project.id
    stage = session.query(TaskStage).filter(TaskStage.task_id == task.id).one()
    assert stage.job_id == job.id
    assert "文生图" in result["reply"]


def test_quick_generate_image_to_image_binds_attachments(quick_db, monkeypatch):
    from app.api.v1 import agent as agent_api

    session, tmp_path = quick_db
    ref_dir = tmp_path / "refs"
    ref_dir.mkdir()
    ref_file = ref_dir / "img1.png"
    ref_file.write_bytes(b"x")
    monkeypatch.setattr(agent_api, "_attachment_root", lambda sid: ref_dir)

    result = agent_api._try_quick_generate(
        session, "ags_quick", "生成一张海边日落的图片",
        [{"id": "img1.png", "name": "img1.png"}],
    )
    assert result is not None
    job = session.get(Job, result["job_id"])
    assert job.params["__media__"] == {"ref_scene": str(ref_file)}
    assert "图生图" in result["reply"]


def test_quick_generate_skips_pipeline_requests(quick_db):
    from app.api.v1 import agent as agent_api

    session, _ = quick_db
    assert agent_api._try_quick_generate(session, "ags_quick", "生成第 3 镜的分镜图", []) is None
    assert agent_api._try_quick_generate(session, "ags_quick", "生成一段视频", []) is None


def test_quick_generate_result_message_has_asset_refs(quick_db):
    """随手生成完成后回写会话消息：气泡据此展示结果图。"""
    from app.api.v1 import agent as agent_api
    from app.models import AgentSession
    from app.models.agent import AgentMessage

    session, _ = quick_db
    session.add(AgentSession(id="ags_quick", title="随手生成", status="active"))
    session.commit()

    result = agent_api._try_quick_generate(session, "ags_quick", "生成一张小狗的图片", [])
    assert result is not None
    session.commit()

    to.bridge_job_event(
        session, job_id=result["job_id"], event_type="job.finished",
        payload={
            "status": "succeeded",
            "result": {
                "assets": [{
                    "asset_id": "ast_q1", "asset_type": "keyframe", "out_name": "quick_1.png",
                }],
            },
        },
    )
    task = session.get(Task, result["task"].id)
    assert task.status == "completed"
    # 终态必须覆盖排队文案，否则任务卡片一直显示「任务已提交，等待执行」
    assert task.current_step == "completed"
    assert task.current_message.startswith("生成完成")
    msg = (
        session.query(AgentMessage)
        .filter(AgentMessage.session_id == "ags_quick", AgentMessage.role == "assistant")
        .one()
    )
    assert msg.payload["asset_refs"][0]["asset_id"] == "ast_q1"


def test_recovery_job_succeeded_closes_task_and_writes_assets(quick_db):
    """恢复/对账路径（job.succeeded，payload 只有统计字段）同样要闭环并回写结果图。"""
    from app.api.v1 import agent as agent_api
    from app.models import AgentSession
    from app.models.agent import AgentMessage

    session, _ = quick_db
    session.add(AgentSession(id="ags_quick", title="随手生成", status="active"))
    session.commit()

    result = agent_api._try_quick_generate(session, "ags_quick", "生成一张小狗的图片", [])
    assert result is not None
    session.commit()
    job_id = result["job_id"]
    job = session.get(Job, job_id)
    # 执行器已把产物落库；恢复路径的终态 payload 只有 {"assets": N}，没有明细
    session.add(
        Asset(
            id="ast_recover1", project_id=job.project_id, asset_type="keyframe",
            name="cover_recover", version=1, source_job_id=job_id,
        )
    )
    session.commit()

    to.bridge_job_event(session, job_id=job_id, event_type="job.succeeded", payload={"assets": 1})

    task = session.get(Task, result["task"].id)
    assert task.status == "completed"
    assert task.current_step == "completed"
    assert task.current_message.startswith("生成完成")
    msg = (
        session.query(AgentMessage)
        .filter(AgentMessage.session_id == "ags_quick", AgentMessage.role == "assistant")
        .one()
    )
    assert msg.payload["asset_refs"][0]["asset_id"] == "ast_recover1"
    assert msg.payload["asset_refs"][0]["has_thumb"] is True
