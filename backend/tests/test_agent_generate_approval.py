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
from app.models import Asset, Base, Job, Project, Scene, Shot, Workflow, ids
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
    video_workflow = Workflow(
        id=ids.workflow_id(),
        name="默认视频工作流",
        workflow_type="video",
        is_default=True,
        is_builtin=True,
        version=1,
        definition={},
    )
    # D-23 资产门禁：场景须绑定有效基准图（文件真实存在），项目审核状态须为 approved
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    baseline_file = media / "baseline.png"
    baseline_file.write_bytes(b"x")
    baseline = Asset(
        id=ids.asset_id(),
        project_id=project.id,
        asset_type="scene",
        name="场景一基准图",
        version=1,
        file_path=str(baseline_file),
        validity="current",
    )
    scene.baseline_asset_id = baseline.id
    project.assets_review_status = "approved"
    # 状态机门禁前置状态：剧本版本 + 评估哈希一致 + parse 任务已成功，
    # 使 derive_progress 推进到 keyframes 阶段
    import hashlib

    from app.models import ScriptVersion

    script_text = "第一场：主角在河边。"
    script_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    script_version = ScriptVersion(
        id=ids.script_version_id(),
        project_id=project.id,
        version=1,
        content_snapshot=script_text,
        content_hash=script_hash,
        created_at=now,
    )
    parse_job = Job(
        id=ids.job_id(),
        project_id=project.id,
        job_type="parse",
        status="succeeded",
        created_at=now,
    )
    session.add_all([project, scene, shot, workflow, video_workflow, baseline, script_version, parse_job])
    session.flush()
    project.current_script_version_id = script_version.id
    project.estimated_text_hash = script_hash
    session.commit()
    yield session, project.id, shot.id
    session.close()


def _job_count(session) -> int:
    """统计生成类任务数（排除夹具中的 parse 前置任务）。"""
    return int(
        session.scalar(
            select(func.count()).select_from(Job).where(Job.job_type != "parse")
        )
        or 0
    )


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


def test_entity_generate_request_and_run(db):
    """实体目标（角色公共资产）：申请只建审批记录，批准后执行建任务并注入归属键。"""
    session, pid, _sid = db
    from app.models import Character

    ch = Character(id=ids.character_id(), project_id=pid, name="主角")
    session.add(ch)
    session.commit()

    requested = execute_tool(
        session,
        "request_generate",
        session_id="ags_t7",
        project_id=pid,
        entity_id=ch.id,
        target="character",
    )
    assert requested.ok, requested.error_message
    assert requested.data["target"] == "character"
    assert _job_count(session) == 0

    confirmation_id = requested.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True, comment="同意")
    run = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert run.ok, run.error_message
    assert run.data["job_type"] == "generate_keyframe"
    job = session.get(Job, run.data["job_id"])
    assert job.params.get("__character_id__") == ch.id
    assert job.shot_id is None


def test_entity_generate_rejects_foreign_entity(db):
    """实体不属于当前项目时拒绝，防跨项目触发生成。"""
    session, pid, _sid = db
    from app.models import Character

    now = datetime.now()
    other = Project(
        id=ids.project_id(),
        name="实体越权项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
    )
    ch = Character(id=ids.character_id(), project_id=other.id, name="别人角色")
    session.add(other)
    session.commit()
    session.add(ch)
    session.commit()

    result = execute_tool(
        session,
        "request_generate",
        session_id="ags_t8",
        project_id=pid,
        entity_id=ch.id,
        target="character",
    )
    assert not result.ok
    assert result.error_code == "entity_not_in_project"
    assert _job_count(session) == 0


def test_clip_blocked_by_keyframe_not_confirmed(db):
    """D-13 闸口：分镜图未人工确认时，Agent 通道 clip 生成被拦下。"""
    session, pid, sid = db
    shot = session.get(Shot, sid)
    assert shot.keyframe_confirmed is False

    requested = execute_tool(
        session,
        "request_generate",
        session_id="ags_t9",
        project_id=pid,
        shot_id=sid,
        target="clip",
    )
    assert requested.ok, requested.error_message
    confirmation_id = requested.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True, comment="同意")

    run = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert not run.ok
    assert run.error_code == "keyframe_not_confirmed"
    assert _job_count(session) == 0


def test_clip_passes_after_keyframe_confirmed(db):
    """D-13 闸口：分镜图人工确认后，Agent 通道 clip 生成放行。"""
    session, pid, sid = db
    shot = session.get(Shot, sid)
    shot.keyframe_confirmed = True
    session.commit()

    requested = execute_tool(
        session,
        "request_generate",
        session_id="ags_t10",
        project_id=pid,
        shot_id=sid,
        target="clip",
    )
    assert requested.ok, requested.error_message
    confirmation_id = requested.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=confirmation_id, approve=True, comment="同意")

    run = execute_tool(session, "run_generate", confirmation_id=confirmation_id, approved=True)
    assert run.ok, run.error_message
    assert run.data["job_type"] == "generate_clip"


def test_list_asset_plans_tool(db):
    """list_asset_plans：返回定义清单与依赖摘要，不泄露文件路径。"""
    session, pid, _sid = db
    from app.models.asset_plan import AssetPlan, AssetPlanDependency

    plan = AssetPlan(
        id=ids.asset_plan_id(),
        project_id=pid,
        plan_type="keyframe",
        asset_key="kf_scene1_01_ab12",
        media_path="data/media/kf_scene1_01_ab12.png",
        status="pending",
        owner_type="shot",
        owner_id=None,
        episode=1,
        seq=1,
    )
    dep = AssetPlanDependency(
        id=ids.asset_plan_dep_id(),
        plan_id=plan.id,
        slot="ref_identity",
        ref_key="chr_zhujue_ab12",
    )
    session.add_all([plan, dep])
    session.commit()

    result = execute_tool(session, "list_asset_plans", project_id=pid)
    assert result.ok, result.error_message
    items = result.data["items"]
    assert len(items) == 1
    item = items[0]
    assert item["plan_id"] == plan.id
    assert item["status"] == "pending"
    assert item["confirmed"] is False
    assert item["dependencies"] == [{"slot": "ref_identity", "ref_key": "chr_zhujue_ab12"}]
    # 不泄露文件系统路径
    assert "media_path" not in item
    assert "thumb_path" not in item

    # 按状态过滤
    pending = execute_tool(session, "list_asset_plans", project_id=pid, status="pending")
    assert pending.ok and len(pending.data["items"]) == 1
    generated = execute_tool(session, "list_asset_plans", project_id=pid, status="generated")
    assert generated.ok and generated.data["count"] == 0
