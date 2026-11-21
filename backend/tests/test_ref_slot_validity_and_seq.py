"""回归测试：参考图槽位解析必须跳过 superseded 旧版本 + 阻断清单镜号不得重复。

两个真实事故（项目「海风与夏日」第 2 集批量出图）：

1. 批量生成报 409「槽位 ref_identity 只能注入角色定妆图，当前文件不是本项目
   任何角色的定妆图」。
   根因：`_resolve_dep_path` 拿到定义上绑定的旧资产后只判断 `file_path` 非空，
   不判断 `validity`。江屿的定妆图被重生成过——v1 `ast_63e5b85ed3ba` 已置为
   superseded，角色指针 `portrait_asset_id` 指向 v2 `ast_43985b693cba`(current)。
   解析器把 superseded 的 v1 交给门禁，门禁按 `portrait_asset_id` 反查 owner
   查不到，判定「不是本项目任何角色的定妆图」，整单阻断。

2. 阻断清单出现 4 组重复的「第 2 集 第 1 镜」。
   根因：`Shot.seq` 是场景内序号，每场从 1 开始。第 2 集有「海岸沙滩」(seq 1~4)
   和「海岸沙滩·夕阳」(seq 1~7) 两场，展示名直接用 seq 拼接就会重复。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset, AssetPlan, AssetPlanDependency, Base, Character, Project, Scene, Shot,
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


def _project(db, name: str = "P") -> Project:
    p = Project(id=f"prj_{name}", name=name, aspect_ratio="9:16")
    db.add(p)
    db.flush()
    return p


# ---------------- 1. superseded 资产不得作为参考图 ----------------


def test_resolve_dep_path_skips_superseded_asset(db, tmp_path):
    """定义绑定的资产已 superseded 时，必须回退到实体当前指针的 current 资产。"""
    from app.api.v1.asset_plans import _resolve_dep_path

    proj = _project(db, "A")
    ch = Character(id="chr_jiangyu", project_id=proj.id, name="江屿")
    db.add(ch)
    db.flush()

    old_file = tmp_path / "old.png"
    new_file = tmp_path / "new.png"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")

    old = Asset(
        id="ast_old", project_id=proj.id, asset_type="character", name="江屿",
        version=1, file_path=str(old_file), validity="superseded",
    )
    new = Asset(
        id="ast_new", project_id=proj.id, asset_type="character", name="江屿",
        version=2, file_path=str(new_file), validity="current",
    )
    db.add_all([old, new])
    db.flush()

    # 角色指针指向 v2
    ch.portrait_asset_id = new.id

    # 角色定义仍绑着 v1（生成 v2 后没有回写定义）—— 这正是事故现场
    plan = AssetPlan(
        id="apn_chr", project_id=proj.id, plan_type="character",
        asset_key="chr_x", media_path=str(old_file), status="generated",
        owner_type="character", owner_id=ch.id, asset_id=old.id,
    )
    db.add(plan)

    shot_plan = AssetPlan(
        id="apn_kf", project_id=proj.id, plan_type="keyframe",
        asset_key="kf_x", media_path=str(tmp_path / "kf.png"), status="pending",
        owner_type="shot", owner_id=None,
    )
    db.add(shot_plan)
    db.flush()

    dep = AssetPlanDependency(
        id="dep_1", plan_id=shot_plan.id, slot="ref_identity",
        ref_key="chr_x", ref_plan_id=plan.id, ref_path=str(old_file),
    )
    db.add(dep)
    db.flush()

    path, asset_id, version = _resolve_dep_path(db, dep)

    assert asset_id == new.id, "必须回退到 current 版本，不能返回 superseded"
    assert version == 2
    assert path == str(new_file)


def test_resolve_dep_path_returns_bound_asset_when_current(db, tmp_path):
    """绑定资产本身是 current 时，正常返回，不做多余回退。"""
    from app.api.v1.asset_plans import _resolve_dep_path

    proj = _project(db, "B")
    f = tmp_path / "cur.png"
    f.write_bytes(b"x")

    cur = Asset(
        id="ast_cur", project_id=proj.id, asset_type="character", name="林晚",
        version=1, file_path=str(f), validity="current",
    )
    db.add(cur)
    db.flush()

    plan = AssetPlan(
        id="apn_c", project_id=proj.id, plan_type="character",
        asset_key="chr_c", media_path=str(f), status="generated",
        owner_type="character", owner_id=None, asset_id=cur.id,
    )
    db.add(plan)

    kf = AssetPlan(
        id="apn_kf2", project_id=proj.id, plan_type="keyframe",
        asset_key="kf_c", media_path=str(tmp_path / "kf2.png"), status="pending",
        owner_type="shot", owner_id=None,
    )
    db.add(kf)
    db.flush()

    dep = AssetPlanDependency(
        id="dep_2", plan_id=kf.id, slot="ref_identity",
        ref_key="chr_c", ref_plan_id=plan.id, ref_path=str(f),
    )
    db.add(dep)
    db.flush()

    path, asset_id, version = _resolve_dep_path(db, dep)
    assert asset_id == cur.id
    assert path == str(f)


# ---------------- 2. 阻断清单镜号必须集内唯一 ----------------


def test_stage_blockers_use_episode_global_seq(db):
    """一集两个场景、各从 seq=1 起：展示名必须是集内全局镜号，不得重复。"""
    from app.services.stage_readiness import check_storyboard_images_ready

    proj = _project(db, "C")

    scene_a = Scene(id="scn_a", project_id=proj.id, title="海岸沙滩", seq=1,
                    description="d")
    scene_b = Scene(id="scn_b", project_id=proj.id, title="海岸沙滩·夕阳", seq=2,
                    description="d")
    db.add_all([scene_a, scene_b])
    db.flush()

    for scene, prefix in ((scene_a, "a"), (scene_b, "b")):
        for i in (1, 2):
            db.add(Shot(
                id=f"sht_{prefix}{i}", scene_id=scene.id, episode=2, seq=i,
                shot_kind="visual", duration=5, status="pending",
            ))
    db.flush()

    readiness = check_storyboard_images_ready(db, proj.id)

    names = [b.name for b in readiness.blockers]
    assert len(names) == len(set(names)), f"镜号重复：{names}"
    assert names == [
        "第 2 集 第 1 镜",
        "第 2 集 第 2 镜",
        "第 2 集 第 3 镜",
        "第 2 集 第 4 镜",
    ]
    assert readiness.total == 4
    assert readiness.done == 0
