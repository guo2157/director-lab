"""三角色（及以上）端到端：槽位分配、提示词注入、依赖落库必须全部覆盖。

用户问题：「如果有三个角色，系统会自动匹配角色槽位和提示词吗？」

## 调查发现的三个硬编码断点

1. ``asset_plans._REF_SLOTS`` 只列到 ``ref_character_2``，
   ``_inject_dependency_slots`` 用它过滤依赖 → ``ref_character_3`` 被静默丢弃，
   第三个人拿不到参考图。
2. ``asset_plans._KEYFRAME_ALLOWED_SLOTS`` 允许 6 个角色槽，两边不一致。
3. ``ref_inject`` 里 ``_CHARACTER_SLOTS = ["ref_identity", "ref_character_2"]``
   是死代码，但容易被人拿去当权威表。

修复后：槽位数量完全由工作流 capabilities 声明决定，代码不再写死上限。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset, AssetPlan, AssetPlanDependency, Base, Character, Project, Scene, Shot, Workflow,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


# ---------------- 1. 注入白名单必须覆盖 ref_character_3..N ----------------


@pytest.mark.parametrize("slot", [
    "ref_identity", "ref_character_2", "ref_character_3", "ref_character_4",
    "ref_character_5", "ref_character_6", "ref_scene", "ref_prop", "ref_image",
])
def test_injectable_slot_accepts_all_character_slots(slot):
    """任意编号的角色槽位都必须可注入（原来只认到 ref_character_2）。"""
    from app.api.v1.asset_plans import _is_injectable_slot

    assert _is_injectable_slot(slot) is True, f"{slot} 被错误过滤"


def test_injectable_slot_rejects_system_slots():
    """系统槽位由执行器回填，@引用不得占用。"""
    from app.api.v1.asset_plans import _is_injectable_slot

    for slot in ("ref_prev_frame", "first_frame", "end_frame", "", None):
        assert _is_injectable_slot(slot) is False


# ---------------- 2. 三角色依赖绑定 ----------------


def _three_char_setup(db, tmp_path):
    """三个角色 + 一个场景 + 一个三人同框镜头。"""
    proj = Project(id="prj_3c", name="三人", aspect_ratio="9:16")
    db.add(proj)
    db.flush()
    names = ["江屿", "林晚", "苏晚"]
    chars = []
    for i, name in enumerate(names):
        ch = Character(id=f"chr_{i}", project_id=proj.id, name=name)
        db.add(ch)
        f = tmp_path / f"{name}.png"
        f.write_bytes(b"x")
        asset = Asset(
            id=f"ast_{i}", project_id=proj.id, asset_type="character",
            name=name, version=1, file_path=str(f), validity="current",
        )
        db.add(asset)
        db.flush()
        ch.portrait_asset_id = asset.id
        plan = AssetPlan(
            id=f"apn_chr{i}", project_id=proj.id, plan_type="character",
            asset_key=f"chr_key_{i}", media_path=str(f), status="generated",
            owner_type="character", owner_id=ch.id, asset_id=asset.id,
        )
        db.add(plan)
        chars.append(ch)

    scene = Scene(id="scn_3c", project_id=proj.id, title="海岸沙滩", seq=1,
                  description="d")
    db.add(scene)
    db.flush()
    sf = tmp_path / "scene.png"
    sf.write_bytes(b"x")
    sasset = Asset(id="ast_scn", project_id=proj.id, asset_type="scene",
                   name="海岸沙滩", version=1, file_path=str(sf), validity="current")
    db.add(sasset)
    db.flush()
    scene.baseline_asset_id = sasset.id
    db.add(AssetPlan(
        id="apn_scn", project_id=proj.id, plan_type="scene", asset_key="scn_key",
        media_path=str(sf), status="generated", owner_type="scene",
        owner_id=scene.id, asset_id=sasset.id,
    ))

    # 三人同框：第二、三人只出现在 beats 动作描述里
    shot = Shot(
        id="sht_3c", scene_id=scene.id, episode=1, seq=1,
        character_id=chars[0].id, shot_kind="visual", duration=5, status="pending",
        dialogue_segments=[],
        beats=[
            {"action": "江屿站在画面左侧"},
            {"action": "林晚在江屿右侧并排站立"},
            {"action": "苏晚在两人身后偏右位置站立"},
        ],
        image_prompt="任务：生成第 1-1 镜的单帧分镜图",
    )
    db.add(shot)
    db.flush()
    return proj, shot, chars, scene


def test_three_characters_all_bound_to_slots(db, tmp_path):
    """三人同框必须绑出 3 个角色槽位，不能只绑前两个。"""
    from app.services.asset_plan import build_asset_plans

    proj, shot, chars, _scene = _three_char_setup(db, tmp_path)
    build_asset_plans(db, proj, episode_start=1, episode_count=1)
    db.commit()

    plan = (
        db.query(AssetPlan)
        .filter(AssetPlan.owner_id == shot.id, AssetPlan.plan_type == "keyframe")
        .first()
    )
    assert plan is not None
    deps = (
        db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id)
        .all()
    )
    slots = {d.slot for d in deps}

    assert "ref_identity" in slots
    assert "ref_character_2" in slots
    assert "ref_character_3" in slots, f"第三个角色槽位缺失，实际 {sorted(slots)}"
    # 三个角色槽必须指向三个不同实体
    char_deps = [d for d in deps if d.slot.startswith(("ref_identity", "ref_character"))]
    assert len({d.ref_key for d in char_deps}) == 3


def test_three_characters_all_injected(db, tmp_path):
    """注入阶段不能把 ref_character_3 过滤掉（原 _REF_SLOTS 的 bug）。"""
    from app.services.asset_plan import build_asset_plans
    from app.api.v1.asset_plans import _inject_dependency_slots

    proj, shot, _chars, _scene = _three_char_setup(db, tmp_path)
    build_asset_plans(db, proj, episode_start=1, episode_count=1)
    db.commit()

    plan = (
        db.query(AssetPlan)
        .filter(AssetPlan.owner_id == shot.id, AssetPlan.plan_type == "keyframe")
        .first()
    )
    params: dict = {}
    injected = _inject_dependency_slots(db, plan, params, None)

    assert "ref_identity" in injected
    assert "ref_character_2" in injected
    assert "ref_character_3" in injected, f"第三个角色未注入，实际 {sorted(injected)}"
    assert len(injected) >= 3


def test_three_characters_all_in_prompt_reference_line(db, tmp_path):
    """提示词「参考资产」行必须列出全部三个角色。"""
    from app.services.shot_refs import collect_shot_characters, inject_asset_refs

    proj, shot, _chars, _scene = _three_char_setup(db, tmp_path)
    db.flush()

    names = collect_shot_characters(db, shot)
    assert names == ["江屿", "林晚", "苏晚"], f"应识别三人，实际 {names}"

    out = inject_asset_refs(db, shot, shot.image_prompt or "")
    head = out.split("\n")[0]
    for name in ("江屿", "林晚", "苏晚"):
        assert f"@{name}" in head, f"{name} 未进入参考资产行：{head}"


# ---------------- 3. 槽位上限：超出工作流声明要明确报错，不能静默丢 ----------------


def test_build_media_params_raises_when_too_many_characters(db, tmp_path):
    """角色数超过工作流声明的槽位数时必须报错，不能静默丢参考图。"""
    from fastapi import HTTPException

    from app.services.ref_inject import build_media_params

    proj, shot, chars, _scene = _three_char_setup(db, tmp_path)
    db.flush()

    prompt = "参考资产：@江屿 @林晚 @苏晚"
    # 工作流只声明 2 个角色槽位
    slots = ["ref_identity", "ref_character_2", "ref_scene"]

    # AppError 是返回 HTTPException 的工厂函数，所以这里捕获 HTTPException
    with pytest.raises(HTTPException) as exc:
        build_media_params(db, proj.id, prompt, slots)
    assert exc.value.status_code == 422
    assert "仅支持" in str(exc.value.detail)


def test_build_media_params_fills_all_declared_slots(db, tmp_path):
    """工作流声明 3 个角色槽位时，三个角色都要注入。"""
    from app.services.ref_inject import build_media_params

    proj, shot, chars, _scene = _three_char_setup(db, tmp_path)
    db.flush()

    prompt = "参考资产：@江屿 @林晚 @苏晚 @海岸沙滩"
    slots = ["ref_scene", "ref_identity", "ref_character_2", "ref_character_3"]

    media = build_media_params(db, proj.id, prompt, slots)

    assert media.get("ref_identity")
    assert media.get("ref_character_2")
    assert media.get("ref_character_3"), f"第三个角色未注入，实际 {sorted(media)}"
    assert len(media) == 4


# ---------------- 4. 角色离场后旧槽位必须清理（否则门禁 409）----------------


def test_stale_character_slot_pruned_when_character_leaves(db, tmp_path):
    """三人镜改成两人后，ref_character_3 必须被清理。

    不清理会导致：注入时把已离场角色的定妆图塞进身份槽 →
    门禁 audit_reference_slots 判 identity_reference_leak → 整单 409。
    """
    from app.services.asset_plan import build_asset_plans

    proj, shot, chars, _scene = _three_char_setup(db, tmp_path)
    build_asset_plans(db, proj, episode_start=1, episode_count=1)
    db.commit()

    plan = (
        db.query(AssetPlan)
        .filter(AssetPlan.owner_id == shot.id, AssetPlan.plan_type == "keyframe")
        .first()
    )
    slots_before = {
        d.slot for d in db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id).all()
    }
    assert "ref_character_3" in slots_before, "前置条件：三人镜应有第三槽位"

    # 剧本改成两人：苏晚离场
    shot.beats = [
        {"action": "江屿站在画面左侧"},
        {"action": "林晚在江屿右侧并排站立"},
    ]
    db.flush()
    build_asset_plans(db, proj, episode_start=1, episode_count=1)
    db.commit()

    slots_after = {
        d.slot for d in db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id).all()
    }

    assert "ref_character_3" not in slots_after, (
        f"离场角色的槽位必须清理，仍有 {sorted(slots_after)}"
    )
    assert {"ref_identity", "ref_character_2"}.issubset(slots_after)


def test_prune_keeps_ref_prev_frame(db, tmp_path):
    """ref_prev_frame 由执行器回填，不能被本轮清理误删。"""
    from app.models import AssetPlanDependency as Dep

    proj, shot, _chars, _scene = _three_char_setup(db, tmp_path)
    from app.services.asset_plan import build_asset_plans
    build_asset_plans(db, proj, episode_start=1, episode_count=1)
    db.commit()

    clip_plan = (
        db.query(AssetPlan)
        .filter(AssetPlan.owner_id == shot.id, AssetPlan.plan_type == "clip")
        .first()
    )
    # 模拟执行器回填的尾帧依赖
    db.add(Dep(
        id="dep_prev", plan_id=clip_plan.id, slot="ref_prev_frame",
        ref_key="last_frame_x", ref_path="/tmp/lf.png",
    ))
    db.flush()

    build_asset_plans(db, proj, episode_start=1, episode_count=1)
    db.commit()

    kept = db.query(Dep).filter(
        Dep.plan_id == clip_plan.id, Dep.slot == "ref_prev_frame").first()
    assert kept is not None, "ref_prev_frame 不应被清理（执行器回填的生命周期槽）"
