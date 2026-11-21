"""锁定「批量生成不重复渲染已生成产物」（2026-11-21 修复）。

## 事故一：分镜图批量出图重复渲染

用户操作：分镜图页 26 镜已全部生成 → 点「全选」→ 点「批量生成」
结果：26 张图全部重新渲染，覆盖掉已确认的图，白烧 GPU。

根因：`_filter_already_generated` 开头是

    if target != "clip":
        return shots, []

只对分镜视频判重，keyframe 完全不判重。批量出视频有保护、批量出图没有。

## 事故二：资产库实体资产重复渲染

资产库批量走的是逐个调用 `/characters/{id}/generate` 等单实体接口，
这些接口只查「在途任务」（_check_asset_inflight），不查「已生成」。
于是点「全选」→「批量生成」，已生成好的定妆图/基准图/锚定图被全部重做。

修复分两层：
- 后端 `_filter_already_generated` 支持 keyframe；新增 `_filter_entity_generated`
- 前端 `AssetsView` 提交前用 `done` 字段过滤（单实体接口无法自带判重）

## 判定口径

同一工作流 + 当前有效产物（validity=current）+ 来源任务 succeeded → 跳过。
换工作流重做是合法的主动行为，不跳过。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset, Base, Character, Job, Project, Scene, Shot, Workflow, ids,
)


def _env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'idem.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _workflow(session, wf_id="wf_a"):
    """Job.workflow_id 有外键约束，必须先建 Workflow 行。

    注意 workflow.definition 是 NOT NULL，必须给一个最小可用的定义 dict。
    """
    wf = Workflow(id=wf_id, name=f"测试工作流 {wf_id}", workflow_type="image",
                  model_type="image_text_to_image",
                  definition={"nodes": [], "links": []})
    session.add(wf)
    session.flush()
    return wf


def _project(session):
    now = datetime.now()
    p = Project(id=ids.project_id(), name="幂等测试", aspect_ratio="9:16",
                creation_type="script", status="active",
                created_at=now, updated_at=now)
    session.add(p)
    session.flush()
    return p


def _shot_with_asset(session, project, *, ep=1, seq=1, wf_id="wf_a",
                     asset_type="keyframe", validity="current",
                     job_status="succeeded"):
    """建镜头 + 一条已生成产物，返回 (shot, asset)。"""
    sc = Scene(id=ids.scene_id(), project_id=project.id, episode=ep, seq=seq,
               title=f"场景{seq}", normalized_name=f"场景{seq}")
    session.add(sc)
    session.flush()
    shot = Shot(id=ids.new_id("sht"), scene_id=sc.id, episode=ep, seq=seq,
                shot_kind="visual", duration=3)
    session.add(shot)
    session.flush()
    _workflow(session, wf_id)
    job = Job(id=ids.new_id("job"), project_id=project.id, shot_id=shot.id,
              job_type=f"generate_{asset_type}", status=job_status,
              workflow_id=wf_id, created_at=datetime.now())
    session.add(job)
    session.flush()
    asset = Asset(id=ids.new_id("ast"), project_id=project.id,
                  asset_type=asset_type, validity=validity,
                  name=f"测试产物{seq}", version=1,
                  source_job_id=job.id, file_path="/tmp/x.png",
                  input_snapshot={})
    session.add(asset)
    session.flush()
    return shot, asset


def test_keyframe_already_generated_is_skipped(tmp_path):
    """分镜图已由同一工作流生成 → 批量出图必须跳过。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    shot, _ = _shot_with_asset(session, project, asset_type="keyframe", wf_id="wf_a")
    session.commit()

    from app.api.v1.generate import _filter_already_generated
    ok, skipped = _filter_already_generated(session, [shot], "keyframe", "wf_a", None)
    assert ok == [], "已生成的分镜图不应再次提交"
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "already_generated"


def test_keyframe_other_workflow_is_allowed(tmp_path):
    """换工作流重做是合法主动行为，不能跳过。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    shot, _ = _shot_with_asset(session, project, asset_type="keyframe", wf_id="wf_a")
    _workflow(session, "wf_b")
    session.commit()

    from app.api.v1.generate import _filter_already_generated
    ok, skipped = _filter_already_generated(session, [shot], "keyframe", "wf_b", None)
    assert len(ok) == 1, "不同工作流应放行"
    assert skipped == []


def test_keyframe_stale_asset_is_allowed(tmp_path):
    """产物已失效（validity=stale）时不算已生成。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    shot, _ = _shot_with_asset(session, project, asset_type="keyframe",
                               validity="stale", wf_id="wf_a")
    session.commit()

    from app.api.v1.generate import _filter_already_generated
    ok, skipped = _filter_already_generated(session, [shot], "keyframe", "wf_a", None)
    assert len(ok) == 1, "失效产物应允许重新生成"
    assert skipped == []


def test_keyframe_failed_job_is_allowed(tmp_path):
    """来源任务失败时不算已生成（否则失败的镜头永远无法重试）。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    shot, _ = _shot_with_asset(session, project, asset_type="keyframe",
                               job_status="failed", wf_id="wf_a")
    session.commit()

    from app.api.v1.generate import _filter_already_generated
    ok, skipped = _filter_already_generated(session, [shot], "keyframe", "wf_a", None)
    assert len(ok) == 1, "失败任务产物应允许重新生成"
    assert skipped == []


def test_clip_filter_still_works(tmp_path):
    """回归：分镜视频的既有判重行为不能被改坏。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    shot, _ = _shot_with_asset(session, project, asset_type="clip", wf_id="wf_a")
    session.commit()

    from app.api.v1.generate import _filter_already_generated
    ok, skipped = _filter_already_generated(session, [shot], "clip", "wf_a", None)
    assert ok == []
    assert len(skipped) == 1


def test_mixed_batch_only_new_shots_submitted(tmp_path):
    """混合批次：只提交未生成的，已生成的进 skipped。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    done_shot, _ = _shot_with_asset(session, project, seq=1, wf_id="wf_a")
    new_shot = Shot(id=ids.new_id("sht"), scene_id=done_shot.scene_id,
                    episode=1, seq=2, shot_kind="visual", duration=3)
    session.add(new_shot)
    session.commit()

    from app.api.v1.generate import _filter_already_generated
    ok, skipped = _filter_already_generated(
        session, [done_shot, new_shot], "keyframe", "wf_a", None)
    assert [s.id for s in ok] == [new_shot.id]
    assert len(skipped) == 1


# ---------------------------------------------------------------- 实体资产

def _character_with_portrait(session, project, *, wf_id="wf_a",
                             validity="current", job_status="succeeded"):
    now = datetime.now()
    ch = Character(id=ids.character_id(), project_id=project.id, name="江屿",
                   normalized_name="江屿", created_at=now, updated_at=now)
    session.add(ch)
    session.flush()
    _workflow(session, wf_id)
    job = Job(id=ids.new_id("job"), project_id=project.id, job_type="generate_keyframe",
              status=job_status, workflow_id=wf_id, created_at=now)
    session.add(job)
    session.flush()
    asset = Asset(id=ids.new_id("ast"), project_id=project.id,
                  asset_type="character", validity=validity,
                  name="江屿定妆图", version=1,
                  source_job_id=job.id, file_path="/tmp/c.png",
                  input_snapshot={})
    session.add(asset)
    session.flush()
    ch.portrait_asset_id = asset.id
    session.commit()
    return ch


def test_entity_with_portrait_is_skipped(tmp_path):
    """已有定妆图（同工作流）→ 批量生成必须跳过。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    ch = _character_with_portrait(session, project, wf_id="wf_a")

    from app.api.v1.generate import _filter_entity_generated
    ok, skipped = _filter_entity_generated(session, [ch], "character", "wf_a", None)
    assert ok == [], "已生成定妆图的角色不应重复生成"
    assert skipped[0]["entity_id"] == ch.id
    assert skipped[0]["reason"] == "already_generated"


def test_entity_other_workflow_is_allowed(tmp_path):
    """换工作流重做实体资产应放行。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    ch = _character_with_portrait(session, project, wf_id="wf_a")
    _workflow(session, "wf_b")
    session.commit()

    from app.api.v1.generate import _filter_entity_generated
    ok, skipped = _filter_entity_generated(session, [ch], "character", "wf_b", None)
    assert len(ok) == 1
    assert skipped == []


def test_entity_without_pointer_is_allowed(tmp_path):
    """没有定妆图指针的角色必须放行（首次生成）。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    now = datetime.now()
    ch = Character(id=ids.character_id(), project_id=project.id, name="林晚",
                   normalized_name="林晚", created_at=now, updated_at=now)
    session.add(ch)
    session.commit()

    from app.api.v1.generate import _filter_entity_generated
    ok, skipped = _filter_entity_generated(session, [ch], "character", "wf_a", None)
    assert len(ok) == 1
    assert skipped == []


def test_entity_stale_portrait_is_allowed(tmp_path):
    """定妆图已失效时应允许重新生成。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    ch = _character_with_portrait(session, project, validity="stale")

    from app.api.v1.generate import _filter_entity_generated
    ok, skipped = _filter_entity_generated(session, [ch], "character", "wf_a", None)
    assert len(ok) == 1
    assert skipped == []


def test_no_workflow_id_means_no_filtering(tmp_path):
    """工作流未知时无法判断，必须全放行（不能误挡正常生成）。"""
    maker = _env(tmp_path)
    session = maker()
    project = _project(session)
    ch = _character_with_portrait(session, project, wf_id="wf_a")

    from app.api.v1.generate import _filter_entity_generated
    ok, skipped = _filter_entity_generated(session, [ch], "character", None, None)
    assert len(ok) == 1
    assert skipped == []


# ---------------------------------------------------------------- 提示文案

def test_already_generated_message_differs_by_target():
    """文案必须按 target 分开：分镜图去镜头卡片重画，分镜视频去详情页重做。

    旧实现两处都写死「生成过视频…请在分镜视频详情页重新生成」，
    keyframe 也走该分支后会把用户指向错误页面。
    """
    from app.api.v1.generate import _already_generated_message
    kf = _already_generated_message("keyframe", 26)
    clip = _already_generated_message("clip", 26)
    assert "分镜图" in kf and "视频" not in kf
    assert "重新生成" in kf
    assert "分镜视频" in clip
    assert kf != clip
    assert "26" in kf and "26" in clip
