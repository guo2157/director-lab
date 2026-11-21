"""锁定「回放历史解析任务不得删除其他任务的镜头」（2026-11-21 修复）。

## 事故（prj_9db2e016b337「海风与夏日」，2026-11-21 10:46）

确认解析版本时，后端把同一 ParseVersion 下**所有**成功任务按 `episode_start`
升序逐个 `apply_parse_result` 回放。该项目有 7 个成功任务：
第 1 集被重解析 6 次 + 第 2 集 1 次。

旧实现在第 5 步删旧镜头时用：

    Scene.id.in_(current_ids)      # current_ids = 项目**全部**场景

于是回放第 1 个任务（第 1 集）时，删除范围覆盖了项目所有场景在该集内的镜头，
其中就包括**后续任务才会重建**的那些。实测后果：

    镜头 17 → 7        （10 个镜头被误删）
    场景  4 → 10       （同名场景 normalized_name 匹配失败，重复创建）

整个过程零报错，用户只看到「分镜变少了」。

## 修复

删除范围收敛到「本次 parse_result 里真正出现过的场景」
（循环内累积的 `_touched_scene_ids`），不再使用项目级 `current_ids`。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Base, ParseVersion, Project, Scene, SceneEpisode, ScriptVersion, Shot, ids,
)
from app.services.script_parse import apply_parse_result


def _env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'replay.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _project_with_two_scenes(maker):
    """建项目：场景 A（第1集）+ 场景 B（第2集），各自已有镜头。"""
    session = maker()
    now = datetime.now()
    project = Project(id=ids.project_id(), name="回放测试", aspect_ratio="9:16",
                      creation_type="script", status="active",
                      created_at=now, updated_at=now)
    session.add(project)
    session.flush()
    sv = ScriptVersion(id=ids.new_id("scv"), project_id=project.id, version=1,
                       content_snapshot="正文", content_hash="h1", created_at=now)
    session.add(sv)
    session.flush()

    sc_a = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1,
                 title="场景A", normalized_name="场景a")
    sc_b = Scene(id=ids.scene_id(), project_id=project.id, episode=2, seq=2,
                 title="场景B", normalized_name="场景b")
    session.add_all([sc_a, sc_b])
    session.flush()
    session.add(SceneEpisode(id=ids.scene_episode_id(), scene_id=sc_a.id,
                             episode=1, seq_in_episode=1))
    session.add(SceneEpisode(id=ids.scene_episode_id(), scene_id=sc_b.id,
                             episode=2, seq_in_episode=1))
    # 两个场景各有 2 个镜头
    for seq in (1, 2):
        session.add(Shot(id=ids.new_id("sht"), scene_id=sc_a.id, episode=1, seq=seq,
                         shot_kind="visual", duration=3))
        session.add(Shot(id=ids.new_id("sht"), scene_id=sc_b.id, episode=2, seq=seq,
                         shot_kind="visual", duration=3))
    session.commit()
    return session, project, sc_a, sc_b


def _shots_of(session, scene_id, episode):
    return (session.query(Shot)
            .filter(Shot.scene_id == scene_id, Shot.episode == episode)
            .order_by(Shot.seq).all())


def _project_with_two_scenes_of_same_episode(maker):
    """建项目：**同一集**里的两个场景 A、C，各有 2 个镜头。

    这是事故的准确形状——真实项目第 1 集有 4 个场景
    （海滩 / 海岸线 / 浅海 / 海边沙滩），集号相同。
    """
    session = maker()
    now = datetime.now()
    project = Project(id=ids.project_id(), name="同集多场景", aspect_ratio="9:16",
                      creation_type="script", status="active",
                      created_at=now, updated_at=now)
    session.add(project)
    session.flush()
    sv = ScriptVersion(id=ids.new_id("scv"), project_id=project.id, version=1,
                       content_snapshot="正文", content_hash="h1", created_at=now)
    session.add(sv)
    session.flush()

    sc_a = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1,
                 title="场景A", normalized_name="场景a")
    sc_c = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=2,
                 title="场景C", normalized_name="场景c")
    session.add_all([sc_a, sc_c])
    session.flush()
    for sc in (sc_a, sc_c):
        session.add(SceneEpisode(id=ids.scene_episode_id(), scene_id=sc.id,
                                 episode=1, seq_in_episode=sc.seq))
        for seq in (1, 2):
            session.add(Shot(id=ids.new_id("sht"), scene_id=sc.id, episode=1,
                             seq=seq, shot_kind="visual", duration=3))
    session.commit()
    return session, project, sc_a, sc_c


def _parse_result(scene_title: str, episode: int, shot_seqs: list[int]) -> dict:
    return {
        "characters": [], "props": [], "prop_events": [], "episodes": [],
        "scenes": [{
            "episode": episode,
            "title": scene_title,
            "description": "描述",
            "shots": [
                {"episode": episode, "seq": s, "shot_kind": "visual", "duration": 3}
                for s in shot_seqs
            ],
        }],
    }


def test_replay_ep1_does_not_delete_other_scenes_of_same_episode(tmp_path):
    """回放时只含场景A，同属第 1 集但本次未出现的场景C 的镜头必须保留。

    这是事故的**准确**复现条件：真实项目里同一集有多个场景
    （海滩 / 海岸线 / 浅海 / 海边沙滩），第 1 集被重解析 6 次。
    旧实现的删除范围是「项目全部场景 ∩ 本集」，于是回放某个只含部分场景的
    历史任务时，把同集其他场景的镜头全删了 —— 集号过滤保护不到它们，
    因为它们的集号就是本次回放的集号。

    注意：早先版本的测试用「第 1 集 vs 第 2 集」做对照，那是**错的**，
    因为 `Shot.episode` 范围过滤天然保护了第 2 集，回滚修复后测试仍通过。
    """
    maker = _env(tmp_path)
    session, project, sc_a, sc_c = _project_with_two_scenes_of_same_episode(maker)
    current_scene_ids = [sc_a.id, sc_c.id]

    # 回放一个只含场景A的历史任务（同集，但没提到场景C）
    apply_parse_result(
        session, project, _parse_result("场景A", 1, [1, 2]),
        episode_start=1, episode_count=1, current_scene_ids=current_scene_ids,
    )
    session.commit()

    ep_c = _shots_of(session, sc_c.id, 1)
    assert len(ep_c) == 2, (
        f"回放只含场景A的结果，删掉了同集场景C的镜头（剩余 {len(ep_c)} 个）。"
        "删除范围必须收敛到本次 parse_result 实际出现的场景"
    )


def test_replay_still_deletes_stale_shots_within_touched_scene(tmp_path):
    """对照组：本次解析**确实**涉及的场景里，多余旧镜头仍要被删除。

    修复不能矫枉过正 —— 同场景重解析时，seq 从 2 个减到 1 个，第 2 个必须删。
    """
    maker = _env(tmp_path)
    session, project, sc_a, sc_b = _project_with_two_scenes(maker)

    apply_parse_result(
        session, project, _parse_result("场景A", 1, [1]),
        episode_start=1, episode_count=1, current_scene_ids=[sc_a.id, sc_b.id],
    )
    session.commit()

    remaining = _shots_of(session, sc_a.id, 1)
    assert len(remaining) == 1, "同场景内多余的旧镜头应被删除"
    assert remaining[0].seq == 1


def test_replay_does_not_create_duplicate_scenes(tmp_path):
    """重复回放同一结果不得因 normalized_name 匹配失败而重复建场景。"""
    maker = _env(tmp_path)
    session, project, sc_a, sc_b = _project_with_two_scenes(maker)
    before = session.query(Scene).filter(Scene.project_id == project.id).count()

    for _ in range(3):
        apply_parse_result(
            session, project, _parse_result("场景A", 1, [1, 2]),
            episode_start=1, episode_count=1,
            current_scene_ids=[sc_a.id, sc_b.id],
        )
        session.commit()

    after = session.query(Scene).filter(Scene.project_id == project.id).count()
    assert after == before, f"重复回放创建了重复场景（{before} → {after}）"


def test_replay_preserves_other_episode_scene_when_ep_missing(tmp_path):
    """回放第 2 集时第 1 集镜头必须保留（对称验证，防止只修了一个方向）。"""
    maker = _env(tmp_path)
    session, project, sc_a, sc_b = _project_with_two_scenes(maker)

    apply_parse_result(
        session, project, _parse_result("场景B", 2, [1, 2]),
        episode_start=2, episode_count=1, current_scene_ids=[sc_a.id, sc_b.id],
    )
    session.commit()

    ep1 = _shots_of(session, sc_a.id, 1)
    assert len(ep1) == 2, "回放第 2 集删掉了第 1 集的镜头"
