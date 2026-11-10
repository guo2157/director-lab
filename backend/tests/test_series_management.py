"""系列化创作与续集管理测试（Phase 1 核心链路）。

覆盖：
- 系列创建同时建立 Bible V1
- 集号自动推导与唯一冲突
- Bible 只新增版本、旧版本置为非激活
- Context Builder 缺 Bible 时失败（不使用空上下文）
- 剧本确认：事件入账、快照生成、state_version 递增、剧本状态流转
- 乐观锁：旧版本提交被拒绝
- 幂等：同一剧本版本重复确认不重复入账
- 连续性检查：道具持有人冲突被识别为 error
- 剧本状态与生产状态解耦：视频未完成不阻止下一集创作
- 剧情线状态约束：已解决剧情线不能被提案重新打开
"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    ContinuityChangeProposal,
    ContinuitySnapshot,
    PlotThread,
    Project,
    Series,
    SeriesBibleVersion,
    SeriesEpisode,
    SeriesScriptVersion,
    StoryEvent,
)
from app.models import ids
from app.schemas.series import EpisodeCreate, PlotThreadCreate, SeriesCreate
from app.services.series import (
    bible_service,
    context_builder_service,
    episode_service,
    narrative_continuity_service,
    plot_thread_service,
    series_service,
    state_commit_service,
)


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'series.db'}")
    from app.models import Base

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _new_series(db, name="小熊成长记") -> Series:
    return series_service.create_series(db, SeriesCreate(name=name))


def _new_episode(db, series: Series, number: int | None = None) -> SeriesEpisode:
    return episode_service.create_episode(
        db, series.id, EpisodeCreate(episode_number=number, title=f"第{number or '?'}集")
    )


def _new_script(
    db, episode: SeriesEpisode, *, content="场景一：小熊去森林。", events=None
) -> SeriesScriptVersion:
    from sqlalchemy import func

    latest = (
        db.query(func.max(SeriesScriptVersion.version))
        .filter(SeriesScriptVersion.episode_id == episode.id)
        .scalar()
    )
    version = SeriesScriptVersion(
        id=ids.series_script_version_id(),
        episode_id=episode.id,
        version=int(latest or 0) + 1,
        content=content,
        synopsis="小熊去森林",
        status="awaiting_review",
        created_at=__import__("datetime").datetime.now().replace(microsecond=0),
        created_by="test",
        validation_result={"events": events if events is not None else []},
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


# ---------------- 系列与 Bible ----------------


def test_create_series_creates_active_bible_v1(db):
    series = _new_series(db)
    assert series.id.startswith("srs_")
    assert series.status == "active"
    assert series.state_version == 1

    bibles = db.query(SeriesBibleVersion).filter_by(series_id=series.id).all()
    assert len(bibles) == 1
    assert bibles[0].version == 1
    assert bibles[0].is_active is True


def test_bible_save_creates_new_version_and_deactivates_old(db):
    series = _new_series(db)
    bible_service.save_bible_version(
        db, series.id, content={"world": "森林"}, change_summary="补充世界设定", created_by="user"
    )
    rows = bible_service.list_bible_versions(db, series.id)
    assert [r.version for r in rows] == [2, 1]
    assert rows[0].is_active is True
    assert rows[1].is_active is False
    # 历史版本内容不变
    assert rows[1].content == {}


def test_bible_content_never_overwritten(db):
    series = _new_series(db)
    bible_service.save_bible_version(
        db, series.id, content={"a": 1}, change_summary=None, created_by="user"
    )
    bible_service.save_bible_version(
        db, series.id, content={"a": 2}, change_summary=None, created_by="user"
    )
    versions = {r.version: r.content for r in bible_service.list_bible_versions(db, series.id)}
    assert versions[1] == {}
    assert versions[2] == {"a": 1}
    assert versions[3] == {"a": 2}


# ---------------- 剧集 ----------------


def test_episode_number_auto_increments(db):
    series = _new_series(db)
    first = _new_episode(db, series)
    second = _new_episode(db, series)
    assert first.episode_number == 1
    assert second.episode_number == 2
    assert episode_service.next_episode_number(db, series.id) == 3


def test_duplicate_episode_number_rejected(db):
    series = _new_series(db)
    _new_episode(db, series, 1)
    with pytest.raises(Exception) as exc:
        _new_episode(db, series, 1)
    assert "episode_number_conflict" in str(getattr(exc.value, "detail", exc.value))


def test_episode_script_status_independent_from_production_status(db):
    series = _new_series(db)
    ep1 = _new_episode(db, series, 1)
    ep1.script_status = "confirmed"
    ep1.production_status = "running"
    db.commit()

    # 上一集视频未完成，仍可创建下一集并进入剧本创作
    ep2 = _new_episode(db, series, 2)
    assert ep2.script_status == "draft"
    assert ep2.production_status == "not_started"


def test_episode_bind_project_rejects_missing_project(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    with pytest.raises(Exception) as exc:
        episode_service.bind_project(db, ep, "prj_not_exists")
    assert "project_not_found" in str(getattr(exc.value, "detail", exc.value))


# ---------------- Context Builder ----------------


def test_context_build_fails_without_bible(db):
    """没有激活 Bible 时必须失败，禁止用空上下文继续生成。"""
    series = Series(
        id=ids.series_id(),
        name="无设定系列",
        language="zh-CN",
        status="active",
        mainline_id="main",
        state_version=1,
    )
    db.add(series)
    db.commit()
    with pytest.raises(Exception) as exc:
        context_builder_service.build_context(
            db,
            series=series,
            target_episode_number=1,
            user_instruction="继续",
            constraints=None,
        )
    assert "series_context_unavailable" in str(getattr(exc.value, "detail", exc.value))


def test_context_contains_bible_and_has_hash(db):
    series = _new_series(db)
    context = context_builder_service.build_context(
        db,
        series=series,
        target_episode_number=1,
        user_instruction="写一个森林探险故事",
        constraints={"reuse_assets": True},
    )
    assert context["series_bible"]["version"] == 1
    assert context["target_episode_number"] == 1
    assert context["user_instruction"] == "写一个森林探险故事"
    assert len(context["context_hash"]) == 64
    # 首集无上一集
    assert context["previous_episode"] is None


def test_context_manifest_persisted_with_incrementing_version(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    m1 = context_builder_service.build_and_persist_for_episode(
        db, series=series, episode=ep, user_instruction="第一版要求", model="m"
    )
    db.commit()
    m2 = context_builder_service.build_and_persist_for_episode(
        db, series=series, episode=ep, user_instruction="第二版要求", model="m"
    )
    db.commit()
    assert m1.context_version == 1
    assert m2.context_version == 2
    assert ep.context_manifest_id == m2.id
    assert m2.manifest_json["user_instruction"] == "第二版要求"


def test_context_warns_when_previous_episode_unconfirmed(db):
    series = _new_series(db)
    ep1 = _new_episode(db, series, 1)
    ep2 = _new_episode(db, series, 2)
    assert ep1.script_status == "draft"
    manifest = context_builder_service.build_and_persist_for_episode(
        db, series=series, episode=ep2, user_instruction="继续第二集"
    )
    codes = {m["code"] for m in manifest.manifest_json["missing"] + manifest.manifest_json["warnings"]}
    assert "previous_episode_not_confirmed" in codes


# ---------------- 剧本确认与状态提交 ----------------


def test_confirm_script_commits_events_and_snapshot(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    events = [
        {
            "event_key": "ev_001",
            "event_type": "character_appearance",
            "title": "小熊出场",
            "description": "小熊在森林入口出现",
            "importance": 4,
        }
    ]
    script = _new_script(db, ep, events=events)

    result = state_commit_service.confirm_script(
        db,
        series=series,
        episode=ep,
        script_version=script,
        expected_state_version=1,
        proposal={"events": events},
        review_note="确认第一集",
        created_by="user",
    )

    assert result["script_status"] == "confirmed"
    assert result["series_state_version"] == 2
    assert result["snapshot_version"] == 1
    assert result["committed_event_count"] == 1

    db.refresh(series)
    db.refresh(ep)
    db.refresh(script)
    assert series.state_version == 2
    assert series.latest_confirmed_episode_id == ep.id
    assert ep.script_status == "confirmed"
    assert ep.confirmed_script_version_id == script.id
    assert script.status == "confirmed"

    stored = db.query(StoryEvent).filter_by(episode_id=ep.id).all()
    assert len(stored) == 1
    assert stored[0].fact_status == "confirmed"
    assert stored[0].event_key == "ev_001"

    snapshots = db.query(ContinuitySnapshot).filter_by(series_id=series.id).all()
    assert len(snapshots) == 1
    assert snapshots[0].version == 1



def test_confirm_rejects_stale_state_version(db):
    series = _new_series(db)
    ep1 = _new_episode(db, series, 1)
    s1 = _new_script(db, ep1, events=[{"event_key": "e1", "event_type": "other", "title": "t1"}])
    state_commit_service.confirm_script(
        db,
        series=series,
        episode=ep1,
        script_version=s1,
        expected_state_version=1,
        proposal={"events": [{"event_key": "e1", "event_type": "other", "title": "t1"}]},
        review_note=None,
    )

    ep2 = _new_episode(db, series, 2)
    s2 = _new_script(db, ep2, events=[{"event_key": "e2", "event_type": "other", "title": "t2"}])
    with pytest.raises(Exception) as exc:
        state_commit_service.confirm_script(
            db,
            series=series,
            episode=ep2,
            script_version=s2,
            expected_state_version=1,  # 陈旧版本
            proposal={"events": [{"event_key": "e2", "event_type": "other", "title": "t2"}]},
            review_note=None,
        )
    assert "series_state_version_conflict" in str(getattr(exc.value, "detail", exc.value))


def test_confirm_is_idempotent_for_same_script_version(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    events = [{"event_key": "e1", "event_type": "other", "title": "t1"}]
    script = _new_script(db, ep, events=events)
    state_commit_service.confirm_script(
        db,
        series=series,
        episode=ep,
        script_version=script,
        expected_state_version=1,
        proposal={"events": events},
        review_note=None,
    )
    # 再次确认同一版本：幂等回放，不重复入账
    second = state_commit_service.confirm_script(
        db,
        series=series,
        episode=ep,
        script_version=script,
        expected_state_version=1,
        proposal={"events": events},
        review_note=None,
    )
    assert second["idempotent_replay"] is True
    assert db.query(StoryEvent).filter_by(episode_id=ep.id).count() == 1
    assert db.query(ContinuitySnapshot).filter_by(series_id=series.id).count() == 1


def test_confirm_rejects_events_without_event_key(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    bad_events = [{"event_type": "other", "title": "缺键事件"}]
    script = _new_script(db, ep, events=bad_events)
    with pytest.raises(Exception) as exc:
        state_commit_service.confirm_script(
            db,
            series=series,
            episode=ep,
            script_version=script,
            expected_state_version=1,
            proposal={"events": bad_events},
            review_note=None,
        )
    assert "series_script_status_conflict" in str(getattr(exc.value, "detail", exc.value))


def test_only_one_confirmed_script_version_per_episode(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    first = _new_script(db, ep, events=[{"event_key": "a", "event_type": "other", "title": "A"}])
    state_commit_service.confirm_script(
        db,
        series=series,
        episode=ep,
        script_version=first,
        expected_state_version=1,
        proposal={"events": [{"event_key": "a", "event_type": "other", "title": "A"}]},
        review_note=None,
    )
    # 新版本覆盖确认
    second = _new_script(db, ep, events=[{"event_key": "b", "event_type": "other", "title": "B"}])
    second.version = 2
    db.commit()
    state_commit_service.confirm_script(
        db,
        series=series,
        episode=ep,
        script_version=second,
        expected_state_version=2,
        proposal={"events": [{"event_key": "b", "event_type": "other", "title": "B"}]},
        review_note=None,
    )
    db.refresh(first)
    db.refresh(second)
    confirmed = (
        db.query(SeriesScriptVersion)
        .filter_by(episode_id=ep.id, status="confirmed")
        .all()
    )
    assert len(confirmed) == 1
    assert confirmed[0].id == second.id
    assert first.status == "superseded"


# ---------------- 连续性检查 ----------------


def test_continuity_detects_prop_state_conflict(db):
    series = _new_series(db)
    ep1 = _new_episode(db, series, 1)
    # 先写入一个持有状态快照
    snapshot = ContinuitySnapshot(
        id=ids.continuity_snapshot_id(),
        series_id=series.id,
        mainline_id="main",
        version=1,
        state_json={"props": {"ast_lamp": {"holder_asset_id": "chr_amy"}}},
        state_hash="x",
        created_at=__import__("datetime").datetime.now().replace(microsecond=0),
    )
    db.add(snapshot)
    db.commit()

    result = narrative_continuity_service.check_script_version(
        db,
        series=series,
        episode=ep1,
        proposal={"props": [{"asset_id": "ast_lamp", "holder_asset_id": "chr_bob"}]},
    )
    types = {i["type"] for i in result["issues"]}
    assert "prop_state_conflict" in types
    assert result["status"] == "conflict"


def test_continuity_detects_reopen_resolved_thread(db):
    series = _new_series(db)
    ep1 = _new_episode(db, series, 1)
    thread = plot_thread_service.create_thread(
        db, series.id, PlotThreadCreate(title="丢失的钥匙")
    )
    thread.status = "resolved"
    db.commit()

    result = narrative_continuity_service.check_script_version(
        db,
        series=series,
        episode=ep1,
        proposal={"reopen_plot_thread_ids": [thread.id]},
    )
    types = {i["type"] for i in result["issues"]}
    assert "plot_thread_reopen_conflict" in types


def test_continuity_no_snapshot_is_warning_not_error(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    result = narrative_continuity_service.check_script_version(
        db, series=series, episode=ep, proposal=None
    )
    types = {i["type"] for i in result["issues"]}
    assert "continuity_snapshot_missing" in types
    severities = {i["severity"] for i in result["issues"]}
    assert "error" not in severities


# ---------------- 剧情线 ----------------


def test_plot_thread_status_whitelist(db):
    series = _new_series(db)
    thread = plot_thread_service.create_thread(
        db, series.id, PlotThreadCreate(title="神秘的脚印")
    )
    assert thread.status == "open"

    from app.schemas.series import PlotThreadUpdate

    with pytest.raises(Exception):
        plot_thread_service.update_thread(
            db, thread.id, PlotThreadUpdate(status="done")  # 非法状态
        )


def test_plot_thread_attach_events_does_not_change_status(db):
    series = _new_series(db)
    thread = plot_thread_service.create_thread(
        db, series.id, PlotThreadCreate(title="线索")
    )
    plot_thread_service.attach_related_events(db, thread.id, ["sev_1", "sev_2"])
    db.refresh(thread)
    assert thread.related_event_ids == ["sev_1", "sev_2"]
    assert thread.status == "open"


# ---------------- 系列归档 ----------------


def test_archive_keeps_data_and_blocks_new_episode(db):
    series = _new_series(db)
    ep = _new_episode(db, series, 1)
    series_service.archive_series(db, series.id)
    db.refresh(series)
    assert series.status == "archived"
    # 历史数据未被删除
    assert db.get(SeriesEpisode, ep.id) is not None
    # 归档后禁止新建剧集
    with pytest.raises(Exception) as exc:
        series_service.get_active_series(db, series.id)
    assert "series_script_status_conflict" in str(getattr(exc.value, "detail", exc.value))
