"""锁定「重新生成成功后不再显示旧失败」（2026-11-21 用户实测事故）。

## 事故

「狼来了」项目分镜图页：镜 3 / 镜 4 首轮生成失败（请求超时），用户点
「重新生成」后成功出图、卡片缩略图已更新，但卡片下方仍挂着

    失败：请求超时: The write operation timed out

的红条，顶部还显示「最近失败 2 张」，点「重试失败」会重提已经出好图的镜头。

## 根因

`failed_recent` 只按「近 N 小时 + 状态 failed/lost」取任务，完全不看同目标
之后是否已重新生成成功。前端按镜头去重展示，于是成功的重画永远消不掉旧失败。
`clip-generation-progress`（分镜看板）、`asset-generation-progress`（资产库）
是完全同构的实现，同样中招。

## 判定口径

同一目标存在 finished_at 更晚的 succeeded 任务 → 该条失败已过期，不再上报。
「失败 → 成功 → 再失败」的最后一条失败时间晚于成功，必须保留（那是真失败）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset, Base, Character, Job, Project, Scene, Shot, Workflow, ids,
)


def _env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'progress.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _workflow(session, wf_id="wf_prog"):
    wf = Workflow(id=wf_id, name="进度测试工作流", workflow_type="image",
                  model_type="image_text_to_image",
                  definition={"nodes": [], "links": []})
    session.add(wf)
    session.flush()
    return wf


def _project(session):
    now = datetime.now()
    p = Project(id=ids.project_id(), name="进度测试", aspect_ratio="9:16",
                creation_type="script", status="active",
                created_at=now, updated_at=now)
    session.add(p)
    session.flush()
    return p


def _shot(session, project, *, seq=1):
    sc = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=seq,
               title=f"场景{seq}", normalized_name=f"场景{seq}")
    session.add(sc)
    session.flush()
    shot = Shot(id=ids.new_id("sht"), scene_id=sc.id, episode=1, seq=seq,
                shot_kind="visual", duration=3)
    session.add(shot)
    session.flush()
    return shot


def _job(session, project, *, job_type="generate_keyframe", status="succeeded",
         shot_id=None, params=None, finished_at=None, wf_id="wf_prog",
         error_msg=None):
    job = Job(
        id=ids.new_id("job"), project_id=project.id, shot_id=shot_id,
        job_type=job_type, status=status, workflow_id=wf_id,
        params=params or {},
        error_msg=error_msg,
        created_at=(finished_at or datetime.now()) - timedelta(minutes=5),
        started_at=(finished_at or datetime.now()) - timedelta(minutes=5),
        finished_at=finished_at or datetime.now(),
    )
    session.add(job)
    session.flush()
    return job


def _asset_for(session, project, job, *, asset_type="keyframe", validity="current"):
    a = Asset(id=ids.new_id("ast"), project_id=project.id, asset_type=asset_type,
              validity=validity, name=f"产物-{job.id}", version=1,
              source_job_id=job.id, file_path="/tmp/x.png", input_snapshot={})
    session.add(a)
    session.flush()
    return a


def _keyframe_progress(session, project, hours=1):
    from app.api.v1.progress import keyframe_generation_progress
    return keyframe_generation_progress(project.id, failed_hours=hours, db=session)


def test_keyframe_failure_cleared_after_regenerate_success(tmp_path):
    """复现事故：失败 → 重新生成成功 → 不得再报「最近失败」。"""
    session = _env(tmp_path)()
    project = _project(session)
    shot = _shot(session, project)
    _workflow(session)

    now = datetime.now()
    _job(session, project, status="failed", shot_id=shot.id,
         finished_at=now - timedelta(minutes=20),
         error_msg="请求超时: The write operation timed out")
    ok_job = _job(session, project, status="succeeded", shot_id=shot.id,
                  finished_at=now - timedelta(minutes=5))
    _asset_for(session, project, ok_job)
    session.commit()

    res = _keyframe_progress(session, project)
    assert res["failed_recent_count"] == 0, res["failed_recent"]
    assert res["failed_recent"] == []
    assert res["with_keyframe"] == 1


def test_keyframe_failure_still_reported_without_success(tmp_path):
    """没重画成功就必须继续提示，别把真失败吞掉。"""
    session = _env(tmp_path)()
    project = _project(session)
    shot = _shot(session, project)
    _workflow(session)

    _job(session, project, status="failed", shot_id=shot.id,
         finished_at=datetime.now() - timedelta(minutes=3),
         error_msg="请求超时: The write operation timed out")
    session.commit()

    res = _keyframe_progress(session, project)
    assert res["failed_recent_count"] == 1
    assert res["failed_recent"][0]["shot_id"] == shot.id
    assert "请求超时" in res["failed_recent"][0]["error_msg"]


def test_keyframe_failure_after_success_is_reported(tmp_path):
    """失败 → 成功 → 再失败：最后一次失败比成功更晚，必须保留。"""
    session = _env(tmp_path)()
    project = _project(session)
    shot = _shot(session, project)
    _workflow(session)

    now = datetime.now()
    _job(session, project, status="failed", shot_id=shot.id,
         finished_at=now - timedelta(minutes=30), error_msg="第一次超时")
    ok_job = _job(session, project, status="succeeded", shot_id=shot.id,
                  finished_at=now - timedelta(minutes=20))
    _asset_for(session, project, ok_job)
    _job(session, project, status="failed", shot_id=shot.id,
         finished_at=now - timedelta(minutes=2), error_msg="重画又超时")
    session.commit()

    res = _keyframe_progress(session, project)
    assert res["failed_recent_count"] == 1
    assert res["failed_recent"][0]["error_msg"] == "重画又超时"


def test_clip_failure_cleared_after_regenerate_success(tmp_path):
    """分镜看板同构：视频重画成功后「视频生成失败」角标必须消失。"""
    session = _env(tmp_path)()
    project = _project(session)
    shot = _shot(session, project)
    _workflow(session)

    now = datetime.now()
    _job(session, project, job_type="generate_clip", status="failed",
         shot_id=shot.id, finished_at=now - timedelta(minutes=25),
         error_msg="请求超时: The write operation timed out")
    ok_job = _job(session, project, job_type="generate_clip", status="succeeded",
                  shot_id=shot.id, finished_at=now - timedelta(minutes=4))
    _asset_for(session, project, ok_job, asset_type="clip")
    session.commit()

    from app.api.v1.progress import clip_generation_progress
    res = clip_generation_progress(project.id, failed_hours=1, db=session)
    assert res["failed_recent_count"] == 0, res["failed_recent"]
    assert res["with_clip"] == 1


def test_asset_failure_cleared_after_regenerate_success(tmp_path):
    """资产库同构：角色定妆图重画成功后不再提示旧失败。"""
    session = _env(tmp_path)()
    project = _project(session)
    _workflow(session)
    ch = Character(id=ids.character_id(), project_id=project.id, name="牧童")
    session.add(ch)
    session.flush()

    now = datetime.now()
    params = {"__character_id__": ch.id}
    _job(session, project, status="failed", params=params,
         finished_at=now - timedelta(minutes=22),
         error_msg="请求超时: The write operation timed out")
    ok_job = _job(session, project, status="succeeded", params=params,
                  finished_at=now - timedelta(minutes=6))
    portrait = _asset_for(session, project, ok_job, asset_type="character")
    ch.portrait_asset_id = portrait.id
    session.commit()

    from app.api.v1.progress import asset_generation_progress
    res = asset_generation_progress(project.id, failed_hours=1, db=session)
    assert res["failed_recent_count"] == 0, res["failed_recent"]
    assert res["groups"]["character"]["done"] == 1
