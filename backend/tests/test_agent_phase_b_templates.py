"""Phase B B7 验收：最小计划模板（资产生成 / 分镜图生成）。"""
from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select

from app.models import Asset, Character, Job, Scene, ScriptVersion, Shot, ids
from app.models.agent import AgentPlanStep
from app.services import planner as planner_mod
from app.services.agent_runtime import run_agent_loop
from app.services.goal_manager import get_or_create_goal
from tests.agent_runtime_fixtures import mk_project


def _media_file(name: str) -> str:
    """写一个真实存在的占位图，返回相对 media_dir 的路径（assets_gate 会校验文件存在）。"""
    from app.core.config import get_settings

    media = get_settings().media_dir
    media.mkdir(parents=True, exist_ok=True)
    (media / name).write_bytes(b"x")
    return name


def _resolved_project(db, *, characters: int, with_assets: int = 0, shots: int = 0):
    p = mk_project(db, with_script=True, with_parse=True)
    now = datetime.now()
    script = db.get(ScriptVersion, p.current_script_version_id)
    h = hashlib.sha256(script.content_snapshot.encode("utf-8")).hexdigest()
    script.content_hash = h
    p.estimated_text_hash = h
    db.add(Job(id=ids.job_id(), project_id=p.id, job_type="parse", status="succeeded", created_at=now))
    db.flush()
    scene = Scene(id=ids.scene_id(), project_id=p.id, episode=1, seq=1, title="场景一")
    db.add(scene)
    db.flush()
    for idx in range(characters):
        c = Character(id=ids.character_id(), project_id=p.id, name=f"角色{idx + 1}")
        db.add(c)
        db.flush()
        if idx < with_assets:
            asset = Asset(
                id=ids.asset_id(), project_id=p.id, asset_type="character",
                name=f"角色{idx + 1}定妆图", version=1,
                file_path=_media_file(f"b7_char_{idx + 1}.png"),
                validity="current",
            )
            db.add(asset)
            db.flush()
            c.portrait_asset_id = asset.id
    baseline = Asset(
        id=ids.asset_id(), project_id=p.id, asset_type="scene", name="场景一基准图",
        version=1, file_path=_media_file("b7_scene_baseline.png"), validity="current",
    )
    db.add(baseline)
    db.flush()
    scene.baseline_asset_id = baseline.id
    for idx in range(shots):
        db.add(
            Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=idx + 1,
                 shot_kind="normal", duration=3)
        )
    db.commit()
    return p


def _goal(db, project, *, goal_type: str, scope: str = "all", session: str):
    return get_or_create_goal(
        db,
        session_id=session,
        description=f"{goal_type} 测试",
        project_id=project.id,
        goal_type=goal_type,
        constraints={"scope": scope},
    )


def _steps(db, plan_id):
    return db.scalars(
        select(AgentPlanStep)
        .where(AgentPlanStep.plan_id == plan_id)
        .order_by(AgentPlanStep.step_index)
    ).all()


def test_assets_template_only_missing_entities(db):
    project = _resolved_project(db, characters=3, with_assets=1)
    goal = _goal(db, project, goal_type="generate_assets", session="b7t1")
    db.commit()

    plan, report, meta = planner_mod.build_plan_from_template(
        db, goal_id=goal.goal_id, goal_type="generate_assets", project_id=project.id
    )
    assert report.passed, report.to_dict()
    assert plan.steps[0].tool_name == "project_overview"
    gen_steps = [s for s in plan.steps if s.tool_name == "request_generate"]
    assert len(gen_steps) == 2, [s.step_key for s in plan.steps]
    assert all(s.step_key.startswith("asset:character:") for s in gen_steps)
    assert all(s.depends_on == ["project_overview"] for s in gen_steps)
    assert meta["remaining_count"] == 0


def test_assets_template_all_present_completes(db, rt_sessionmaker):
    project = _resolved_project(db, characters=2, with_assets=2)
    _goal(db, project, goal_type="generate_assets", session="b7t2")
    db.commit()

    result = run_agent_loop(
        db, session_id="b7t2", description="生成所有角色资产",
        project_id=project.id, goal_type="generate_assets",
        constraints={"scope": "character"},
    )
    assert result.status == "completed", result.to_dict()
    assert result.completed_steps == 1  # 只有 overview
    db.expire_all()
    assert result.plan_id
    assert [s.tool_name for s in _steps(db, result.plan_id)] == ["project_overview"]


def test_keyframes_blocked_when_assets_not_ready(db):
    project = _resolved_project(db, characters=2, with_assets=0, shots=2)
    goal = _goal(db, project, goal_type="generate_keyframes", session="b7t3")
    db.commit()

    plan, report, meta = planner_mod.build_plan_from_template(
        db, goal_id=goal.goal_id, goal_type="generate_keyframes", project_id=project.id
    )
    assert report.passed, report.to_dict()
    assert len(plan.steps) == 1
    assert plan.steps[0].step_type == "clarification"
    assert meta["clarification"]


def test_batch_cap_and_continuation(db, rt_sessionmaker, monkeypatch):
    project = _resolved_project(db, characters=35, with_assets=0)
    _goal(db, project, goal_type="generate_assets", session="b7t4")
    db.commit()

    first = run_agent_loop(
        db, session_id="b7t4", description="生成所有角色资产",
        project_id=project.id, goal_type="generate_assets",
        constraints={"scope": "character"},
    )
    # 35 个角色缺资产：第一批只规划 30 个（不关心审批/前置失败，只看计划规模）
    assert first.status in ("waiting_approval", "waiting_user", "partially_completed"), first.to_dict()
    db.expire_all()
    first_steps = _steps(db, first.plan_id)
    assert len([s for s in first_steps if s.tool_name == "request_generate"]) == 30
    assert len(first_steps) == 31  # overview + 30

    # 第一批 30 个实体真实产出资产（模板按项目状态规划，必须反映真实数据），
    # 步骤标 succeeded；下一次执行生成第二个 revision，只含剩余 5 步
    first_entity_ids = {
        s.args.get("entity_id") for s in first_steps if s.tool_name == "request_generate"
    }
    for s in first_steps:
        s.status = "succeeded"
    for cid in first_entity_ids:
        asset = Asset(
            id=ids.asset_id(), project_id=project.id, asset_type="character",
            name=f"{cid} 定妆图", version=1,
            file_path=_media_file(f"b7_cont_{cid}.png"), validity="current",
        )
        db.add(asset)
        db.flush()
        db.get(Character, cid).portrait_asset_id = asset.id
    db.commit()

    second = run_agent_loop(
        db, session_id="b7t4", description="生成所有角色资产",
        project_id=project.id, goal_type="generate_assets",
        constraints={"scope": "character"},
    )
    db.expire_all()
    assert second.plan_id != first.plan_id
    second_steps = _steps(db, second.plan_id)
    assert len([s for s in second_steps if s.tool_name == "request_generate"]) == 5
    # 已完成的 overview 被 carry over，不再重复执行
    overview = next(s for s in second_steps if s.step_key == "project_overview")
    assert overview.status == "succeeded"
    assert (overview.result or {}).get("carried_over_from")


def test_template_steps_pass_validator(db):
    project = _resolved_project(db, characters=2, with_assets=0, shots=2)
    for goal_type in ("generate_assets", "generate_keyframes"):
        _plan, report, _meta = planner_mod.build_plan_from_template(
            db, goal_id="gol_x", goal_type=goal_type, project_id=project.id
        )
        assert report.passed, (goal_type, report.to_dict())
