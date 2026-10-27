"""对话内生成申请的镜头定位测试。

覆盖：
- 镜头 ID 精确匹配、不存在、不属于本项目
- 『第 N 镜』命中唯一、命中多个（必须判为无法确定而不是挑一个）、不存在
- 未给出任何镜头标识时要求澄清，不得猜测
- 生成目标判定：视频类关键词优先，其余默认关键帧
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Project, Scene, Shot, ids
from app.services.generate_intent import resolve_shot, resolve_target


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'shot.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="镜头定位项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    other = Project(
        id=ids.project_id(), name="另一个项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
    scene2 = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=2, title="场景二")
    other_scene = Scene(id=ids.scene_id(), project_id=other.id, episode=1, seq=1, title="别人场景")
    shot1 = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    shot2 = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=2, shot_kind="normal", duration=3)
    # 第 2 镜在另一个场景也出现一次：用于验证「第 N 镜」重复时不得挑一个
    shot2b = Shot(id=ids.shot_id(), scene_id=scene2.id, episode=1, seq=2, shot_kind="normal", duration=3)
    foreign = Shot(id=ids.shot_id(), scene_id=other_scene.id, episode=1, seq=1, shot_kind="normal", duration=3)
    session.add_all([project, other, scene, scene2, other_scene, shot1, shot2, shot2b, foreign])
    session.commit()
    yield session, project.id, shot1.id, foreign.id, shot2.id
    session.close()


def test_resolve_by_shot_id(db):
    """消息中给出镜头 ID 时精确命中。"""
    session, pid, sid, _fid, _s2 = db
    shot, reason = resolve_shot(session, pid, f"给 {sid} 生成关键帧")
    assert reason is None and shot is not None and shot.id == sid


def test_resolve_unknown_shot_id(db):
    """给出不存在的镜头 ID 时返回未找到，不猜测。"""
    session, pid, _sid, _fid, _s2 = db
    shot, reason = resolve_shot(session, pid, "给 sht_notexist0001 生成关键帧")
    assert shot is None and reason == "shot_not_found"


def test_resolve_foreign_shot_rejected(db):
    """镜头存在但不属于当前项目时拒绝，防跨项目生成。"""
    session, pid, _sid, fid, _s2 = db
    shot, reason = resolve_shot(session, pid, f"给 {fid} 生成关键帧")
    assert shot is None and reason == "shot_not_in_project"


def test_resolve_unique_seq(db):
    """『第 1 镜』在项目内唯一时命中。"""
    session, pid, sid, _fid, _s2 = db
    shot, reason = resolve_shot(session, pid, "为第 1 镜生成关键帧")
    assert reason is None and shot is not None and shot.id == sid


def test_resolve_ambiguous_seq_not_guessed(db):
    """『第 2 镜』命中多个时必须判为无法确定，不得擅自挑一个。"""
    session, pid, _sid, _fid, _s2 = db
    shot, reason = resolve_shot(session, pid, "为第 2 镜生成关键帧")
    assert shot is None
    assert reason == "shot_ambiguous"


def test_resolve_missing_seq(db):
    """『第 99 镜』不存在时返回未找到。"""
    session, pid, _sid, _fid, _s2 = db
    shot, reason = resolve_shot(session, pid, "为第 99 镜生成关键帧")
    assert shot is None and reason == "shot_not_found"


def test_resolve_unspecified_requires_clarification(db):
    """消息未给出任何镜头标识时要求澄清，绝不默认选一个镜头。"""
    session, pid, _sid, _fid, _s2 = db
    shot, reason = resolve_shot(session, pid, "帮我生成一个角色图")
    assert shot is None and reason == "shot_unspecified"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("给第 3 镜生成视频片段", "clip"),
        ("来段动态的", "clip"),
        ("给第 3 镜生成关键帧", "keyframe"),
        ("出个分镜图", "keyframe"),
        # 无明确目标时取成本最低的关键帧，避免误判成更贵的视频生成
        ("给第 3 镜生成一下", "keyframe"),
    ],
)
def test_resolve_target(text: str, expected: str):
    assert resolve_target(text) == expected
