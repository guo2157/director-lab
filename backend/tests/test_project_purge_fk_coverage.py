"""回归测试：purge 必须覆盖所有 FK 引用 shot / asset 的子表。

真实事故（2026-11-21，项目「狼来了」）：
purge_project 删除镜头时抛 FOREIGN KEY constraint failed → 500，项目无法删除。
根因：分镜重新生成工作台新增了 storyboard_regen_config 表（shot_id → shot.id），
purge 没覆盖它。同源风险还有 series_assets（asset_id → asset.id，系列共享资产）。

这里用真实外键约束复现：任何引用被删实体的子表漏删，purge 都会在这里炸掉，
而不是等到线上用户点删除才暴露。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset, Base, Project, Scene, Series, SeriesAsset, Shot, StoryboardRegenConfig,
)
from app.services.project_purge import purge_project


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _seed(db):
    p = Project(id="prj_purge", name="狼来了", aspect_ratio="9:16")
    s = Series(id="srs_1", name="系列", created_at=_now())
    db.add_all([p, s])
    db.flush()
    sc = Scene(id="scn_1", project_id=p.id, title="山坡", seq=1, episode=1)
    db.add(sc)
    db.flush()
    sh = Shot(id="sht_1", scene_id=sc.id, seq=1, episode=1)
    a = Asset(id="ast_1", project_id=p.id, asset_type="keyframe", name="分镜图1", version=1)
    db.add_all([sh, a])
    db.flush()
    # 两个 FK 子表：一个漏了都会让 purge 在删 shot / asset 时炸掉
    db.add_all([
        StoryboardRegenConfig(id="rgc_1", project_id=p.id, shot_id=sh.id, target="clip"),
        SeriesAsset(id="sas_1", series_id=s.id, asset_id=a.id, created_at=_now()),
    ])
    db.commit()
    return p.id


def test_purge_cleans_regen_config_and_series_asset(db):
    pid = _seed(db)
    result = purge_project(db, pid)
    db.commit()

    assert result["deleted"]["storyboard_regen_config"] == 1
    assert result["deleted"]["series_asset"] == 1
    assert result["deleted"]["project"] == 1
    assert db.get(Project, pid) is None


def test_purge_idempotent_after_cleanup(db):
    """两次 purge 第二次应抛 LookupError（项目已不存在），而不是 FK 报错。"""
    pid = _seed(db)
    purge_project(db, pid)
    db.commit()
    with pytest.raises(LookupError):
        purge_project(db, pid)
