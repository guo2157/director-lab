"""回归测试：批量生成必须跳过「在途任务」，不能重复建任务。

真实事故（项目「海风与夏日」第 2 集）：11 个分镜图定义中 5 个已生成、6 个正在
生成（queued/running）。用户再次点击「批量生成」→ 又为这 6 个镜头各建了一个
任务，变成 17 个任务 / 11 个镜头，白白排队消耗算力。

根因：`_filter_already_generated` 只查「已成功的产物」（Job.status == succeeded
且 Asset.validity == current）。任务还在 queued/running 时没有任何产物，
判重查不到，于是全部放行。`_filter_entity_generated`（角色/场景/道具）同理。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset, Base, Character, Job, Project, Scene, Shot, Workflow,
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


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _workflow(db, wf_id="wf_1"):
    """job.workflow_id 是外键，测试里必须建真实工作流记录。"""
    wf = db.get(Workflow, wf_id)
    if wf is not None:
        return wf
    wf = Workflow(
        id=wf_id, name=wf_id, workflow_type="image", definition={},
        version=1,
    )
    db.add(wf)
    db.flush()
    return wf


def _project(db, name="P"):
    p = Project(id=f"prj_{name}", name=name, aspect_ratio="9:16")
    db.add(p)
    db.flush()
    return p


def _scene(db, pid, title="沙滩", seq=1):
    s = Scene(id=f"scn_{title}{seq}", project_id=pid, title=title, seq=seq,
              description="d")
    db.add(s)
    db.flush()
    return s


def _shot(db, scene, seq, episode=1):
    s = Shot(id=f"sht_{scene.id}_{seq}", scene_id=scene.id, episode=episode,
             seq=seq, shot_kind="visual", duration=5, status="pending")
    db.add(s)
    db.flush()
    return s


# ---------------- 分镜图/视频：在途任务判重 ----------------


def test_filter_skips_inflight_keyframe_jobs(db):
    """有 queued 任务时，同一镜头必须被判重跳过（reason=already_inflight）。"""
    from app.api.v1.generate import _filter_already_generated

    proj = _project(db, "A")
    _workflow(db)
    scene = _scene(db, proj.id)
    inflight_shot = _shot(db, scene, 1)
    fresh_shot = _shot(db, scene, 2)

    db.add(Job(
        id="job_inflight", project_id=proj.id, shot_id=inflight_shot.id,
        job_type="generate_keyframe", workflow_id="wf_1", workflow_version=1,
        status="queued", params={}, created_at=_now(),
    ))
    db.flush()

    ok, skipped = _filter_already_generated(
        db, [inflight_shot, fresh_shot], "keyframe", "wf_1", 1,
    )

    assert [s.id for s in ok] == [fresh_shot.id], "在途镜头必须被跳过"
    assert len(skipped) == 1
    assert skipped[0]["shot_id"] == inflight_shot.id
    assert skipped[0]["reason"] == "already_inflight"


def test_filter_skips_running_clip_jobs(db):
    """running 状态同样算在途。"""
    from app.api.v1.generate import _filter_already_generated

    proj = _project(db, "B")
    _workflow(db)
    scene = _scene(db, proj.id)
    shot = _shot(db, scene, 1)

    db.add(Job(
        id="job_running", project_id=proj.id, shot_id=shot.id,
        job_type="generate_clip", workflow_id="wf_1", workflow_version=1,
        status="running", params={}, created_at=_now(),
    ))
    db.flush()

    ok, skipped = _filter_already_generated(db, [shot], "clip", "wf_1", 1)

    assert ok == []
    assert skipped[0]["reason"] == "already_inflight"


def test_filter_still_passes_when_no_jobs(db):
    """既没产物也没在途任务 → 全部放行，不能被新逻辑误拦。"""
    from app.api.v1.generate import _filter_already_generated

    proj = _project(db, "C")
    _workflow(db)
    scene = _scene(db, proj.id)
    shot = _shot(db, scene, 1)

    ok, skipped = _filter_already_generated(db, [shot], "keyframe", "wf_1", 1)

    assert [s.id for s in ok] == [shot.id]
    assert skipped == []


def test_filter_prefers_already_generated_over_inflight(db):
    """既有产物又有在途任务时，优先报 already_generated（信息更准确）。"""
    from app.api.v1.generate import _filter_already_generated

    proj = _project(db, "D")
    _workflow(db)
    scene = _scene(db, proj.id)
    shot = _shot(db, scene, 1)

    db.add(Job(
        id="job_ok", project_id=proj.id, shot_id=shot.id,
        job_type="generate_keyframe", workflow_id="wf_1", workflow_version=1,
        status="succeeded", params={}, created_at=_now(),
    ))
    db.flush()
    db.add(Asset(
        id="ast_kf", project_id=proj.id, asset_type="keyframe", name="kf",
        version=1, file_path="/tmp/x.png", validity="current",
        source_job_id="job_ok",
    ))
    # 同时还有一个在途任务
    db.add(Job(
        id="job_new", project_id=proj.id, shot_id=shot.id,
        job_type="generate_keyframe", workflow_id="wf_1", workflow_version=1,
        status="queued", params={}, created_at=_now(),
    ))
    db.flush()

    ok, skipped = _filter_already_generated(db, [shot], "keyframe", "wf_1", 1)

    assert ok == []
    assert skipped[0]["reason"] == "already_generated"


# ---------------- 实体资产：在途任务判重 ----------------


def test_filter_skips_inflight_character_jobs(db):
    """角色定妆图在途时也要跳过，避免重复点击批量生成反复建任务。"""
    from app.api.v1.generate import _filter_entity_generated

    proj = _project(db, "E")
    _workflow(db)
    ch = Character(id="chr_a", project_id=proj.id, name="江屿")
    db.add(ch)
    db.flush()

    db.add(Job(
        id="job_chr", project_id=proj.id, shot_id=None,
        job_type="generate_character", workflow_id="wf_1", workflow_version=1,
        status="queued", params={"__character_id__": ch.id}, created_at=_now(),
    ))
    db.flush()

    ok, skipped = _filter_entity_generated(db, [ch], "character", "wf_1", 1)

    assert ok == []
    assert skipped[0]["reason"] == "already_inflight"
    assert skipped[0]["entity_id"] == ch.id


def test_filter_entity_passes_when_idle(db):
    """没有在途任务也没有产物 → 放行。"""
    from app.api.v1.generate import _filter_entity_generated

    proj = _project(db, "F")
    _workflow(db)
    ch = Character(id="chr_b", project_id=proj.id, name="林晚")
    db.add(ch)
    db.flush()

    ok, skipped = _filter_entity_generated(db, [ch], "character", "wf_1", 1)

    assert [e.id for e in ok] == [ch.id]
    assert skipped == []
