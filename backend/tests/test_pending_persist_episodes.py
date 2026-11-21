"""锁定「已确认版本 + 后续批次未落库」的检测能力（2026-11-21 修复）。

## 事故（prj_9db2e016b337「海风与夏日」）

1. 第 1 集解析成功，前端自动确认 → ParseVersion `pv_1de04377fa44` 变 `current`。
2. 用户点「继续分析」解析第 2 集，任务经 `_find_or_create_parse_version`
   **复用同一个 ParseVersion**（该函数明确兼容 `current`，见其 docstring）。
3. 前端自动确认的判据是 `parse_version_status === 'candidate'`，
   此时状态已是 `current` → `confirmTarget` 恒为 null → `apply_parse_result`
   一次都不会执行。

结果：第 2 集的 11 个分镜留在 `job.result` 里，库里 0 条第 2 集镜头。
解析页显示「第 2 集 11 个分镜」（读 job.result），
分镜图页只有第 1 集（读 shot 表），**全程无任何报错**。

## 修复判据

不能只看 `pv.status`：`current` 表示「确认过一次」，不代表「后续批次已落库」。
必须用事实判断——集级记录里有 `succeeded` 的集，但该集的镜头在 shot 表查不到，
即存在未落库批次 → 写入 `scope.pending_persist_episodes`，
前端据此对 `current` 版本补一次确认（确认接口允许 script_agent 增量落库）。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Base, EpisodeParseRecord, Job, ParseVersion, Project, Scene, SceneEpisode,
    ScriptVersion, Shot, ids,
)


def _env(tmp_path):
    """建一个最小项目：ParseVersion 已 current，含 1 集已落库 + 1 集仅记录。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return maker


def _seed(maker, *, persist_ep2: bool):
    """构造事故现场。

    :param persist_ep2: True 表示第 2 集镜头**已**落库（对照组，应无 pending）
    """
    session = maker()
    now = datetime.now()
    project = Project(id=ids.project_id(), name="多集未落库", aspect_ratio="9:16",
                      creation_type="script", status="active",
                      created_at=now, updated_at=now)
    session.add(project)
    session.flush()

    # ParseVersion.script_version_id 有外键约束，必须先有 ScriptVersion
    sv = ScriptVersion(id=ids.new_id("scv"), project_id=project.id, version=1,
                       content_snapshot="剧本正文", content_hash="h1", created_at=now)
    session.add(sv)
    session.flush()

    pv = ParseVersion(id=ids.parse_version_id(), project_id=project.id,
                      script_version_id=sv.id, version=1,
                      status="current", script_hash="h1", total_episodes=2,
                      created_at=now, confirmed_at=now)
    session.add(pv)
    session.flush()
    project.current_parse_version_id = pv.id

    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1,
                  title="海滩", normalized_name="海滩")
    session.add(scene)
    session.flush()

    # 第 1 集：记录 succeeded + 镜头真实落库
    session.add(SceneEpisode(id=ids.scene_episode_id(), scene_id=scene.id,
                             episode=1, seq_in_episode=1))
    session.add(Shot(id=ids.new_id("sht"), scene_id=scene.id, episode=1, seq=1,
                     shot_kind="visual", duration=3))
    session.add(EpisodeParseRecord(
        id=ids.episode_parse_record_id(), project_id=project.id, episode_no=1,
        asset_type="all", status="succeeded", asset_count=1,
        parse_version_id=pv.id, created_at=now))
    # 第 2 集：记录 succeeded，但（默认）镜头没有落库
    session.add(EpisodeParseRecord(
        id=ids.episode_parse_record_id(), project_id=project.id, episode_no=2,
        asset_type="all", status="succeeded", asset_count=11,
        parse_version_id=pv.id, created_at=now))
    if persist_ep2:
        scene2 = Scene(id=ids.scene_id(), project_id=project.id, episode=2, seq=2,
                       title="海岸沙滩", normalized_name="海岸沙滩")
        session.add(scene2)
        session.flush()
        session.add(SceneEpisode(id=ids.scene_episode_id(), scene_id=scene2.id,
                                 episode=2, seq_in_episode=1))
        session.add(Shot(id=ids.new_id("sht"), scene_id=scene2.id, episode=2, seq=1,
                         shot_kind="visual", duration=3))
    session.commit()
    return session, project, pv


def _status(session, project_id: str) -> dict:
    from app.api.v1.script import get_script_agent_status
    return get_script_agent_status(project_id, db=session)


def test_unpersisted_episode_is_reported(tmp_path):
    """第 2 集记录 succeeded 但库里无镜头 → pending_persist_episodes 必须含 2。"""
    maker = _env(tmp_path)
    session, project, pv = _seed(maker, persist_ep2=False)

    out = _status(session, project.id)
    pending = out["scope"]["pending_persist_episodes"]
    assert pending == [2], f"应只报告第 2 集未落库，实际 {pending}"
    # 版本已是 current —— 这正是旧实现判断失效的原因，必须仍能识别
    assert out["scope"]["parse_version_status"] == "current"


def test_fully_persisted_project_reports_nothing(tmp_path):
    """对照组：两集都落库后 pending 必须为空，否则前端会无限补确认。"""
    maker = _env(tmp_path)
    session, project, pv = _seed(maker, persist_ep2=True)

    out = _status(session, project.id)
    assert out["scope"]["pending_persist_episodes"] == []


def test_first_episode_unpersisted_also_detected(tmp_path):
    """不只看「后续批次」：任何 succeeded 但无镜头的集都要报出来。"""
    maker = _env(tmp_path)
    session, project, pv = _seed(maker, persist_ep2=False)
    # 把第 1 集的镜头也删掉，模拟「一次都没落库」
    session.query(Shot).delete()
    session.commit()

    out = _status(session, project.id)
    assert out["scope"]["pending_persist_episodes"] == [1, 2]


def test_records_of_other_version_are_ignored(tmp_path):
    """集级记录属于旧版本时不应触发补确认（避免把历史版本当成未落库）。

    真实情形：项目重解析后产生新版本，旧版本留下的 succeeded 记录还挂在旧
    parse_version_id 上。这些记录对应的镜头可能早被新一轮解析删除，
    若按「记录 succeeded 但库里无镜头」直接判定，会误报成未落库、反复补确认。
    """
    maker = _env(tmp_path)
    session, project, pv = _seed(maker, persist_ep2=False)
    # 让当前版本成为第 2 版，旧版本为第 1 版（version 大者为最新，见接口排序）
    pv.version = 2
    session.commit()
    other = ParseVersion(id=ids.parse_version_id(), project_id=project.id,
                         script_version_id=session.query(ScriptVersion.id).scalar(),
                         version=1,
                         status="superseded", script_hash="h0", total_episodes=2,
                         created_at=datetime.now())
    session.add(other)
    session.flush()
    rec = (session.query(EpisodeParseRecord)
           .filter(EpisodeParseRecord.episode_no == 2).one())
    rec.parse_version_id = other.id
    session.commit()

    out = _status(session, project.id)
    assert out["scope"]["parse_version_id"] == pv.id
    assert out["scope"]["pending_persist_episodes"] == [], \
        "旧版本记录不应被算作当前版本的未落库集"


def test_pending_clears_after_apply(tmp_path):
    """模拟确认接口落库后 pending 应清空（前端补确认的收敛性）。"""
    maker = _env(tmp_path)
    session, project, pv = _seed(maker, persist_ep2=False)
    assert _status(session, project.id)["scope"]["pending_persist_episodes"] == [2]

    # 补上第 2 集镜头（等价于 apply_parse_result 落库后的状态）
    scene2 = Scene(id=ids.scene_id(), project_id=project.id, episode=2, seq=2,
                   title="海岸沙滩", normalized_name="海岸沙滩")
    session.add(scene2)
    session.flush()
    session.add(SceneEpisode(id=ids.scene_episode_id(), scene_id=scene2.id,
                             episode=2, seq_in_episode=1))
    session.add(Shot(id=ids.new_id("sht"), scene_id=scene2.id, episode=2, seq=1,
                     shot_kind="visual", duration=3))
    session.commit()

    assert _status(session, project.id)["scope"]["pending_persist_episodes"] == []
