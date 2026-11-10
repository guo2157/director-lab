"""系列化创作：剧情事实与连续性状态。

包含四类数据，职责严格区分：
- story_events：已发生的剧情事实（必须关联剧本版本，可追溯来源）
- continuity_snapshots：正式连续性状态的不可变快照（按 version 递增）
- continuity_change_proposals：Agent 产出的状态变更提案（不等于正式状态）
- episode_context_manifests：每次生成剧本时实际使用的上下文清单
- plot_threads：未解决剧情线

注意：本模块与 `models/continuity.py`（跨集视频首尾帧连续性）无关。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ids
from app.models.base import Base

# 事件类型（V1 允许集合，超出集合视为无效输入）
STORY_EVENT_TYPES: tuple[str, ...] = (
    "character_appearance",
    "character_action",
    "character_relationship_change",
    "prop_acquired",
    "prop_lost",
    "prop_transferred",
    "prop_damaged",
    "prop_repaired",
    "scene_discovery",
    "conflict",
    "revelation",
    "promise",
    "secret",
    "plot_development",
    "plot_resolution",
    "other",
)

# 事实状态：只有 confirmed 能作为正式剧情事实
STORY_EVENT_FACT_STATUS: tuple[str, ...] = (
    "confirmed",
    "derived",
    "uncertain",
    "proposed",
    "contradicted",
)

# 剧情线状态
PLOT_THREAD_STATUS: tuple[str, ...] = (
    "open",
    "in_progress",
    "resolved",
    "abandoned",
)

# 提案状态
PROPOSAL_STATUS: tuple[str, ...] = ("pending", "committed", "rejected")


class StoryEvent(Base):
    """剧情事件账本：正式剧情事实的唯一落库位置。

    幂等键 event_key 与 (episode_id, script_version_id) 组成唯一约束，
    保证同一剧本版本重复提交不会产生重复事件。
    """

    __tablename__ = "story_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.story_event_id)
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series_episodes.id"), nullable=False
    )
    script_version_id: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    involved_asset_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    location_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_scene_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="proposed", server_default="proposed"
    )
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    occurred_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "episode_id",
            "script_version_id",
            "event_key",
            name="uq_story_event_episode_version_key",
        ),
    )


Index("ix_story_events_series_episode", StoryEvent.series_id, StoryEvent.episode_id)
Index("ix_story_events_series_fact", StoryEvent.series_id, StoryEvent.fact_status)


class ContinuitySnapshot(Base):
    """连续性状态快照：不可变，按 version 递增，保留全部历史。"""

    __tablename__ = "continuity_snapshots"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.continuity_snapshot_id
    )
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    mainline_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="main", server_default="main"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    based_on_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    based_on_script_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint("series_id", "version", name="uq_continuity_snapshot_series_version"),
    )


Index("ix_continuity_snapshots_series", ContinuitySnapshot.series_id, ContinuitySnapshot.version)


class ContinuityChangeProposal(Base):
    """状态变更提案：Agent 只能写这里，只有 StateCommitService 能提交。"""

    __tablename__ = "continuity_change_proposals"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.continuity_proposal_id
    )
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series_episodes.id"), nullable=False
    )
    script_version_id: Mapped[str] = mapped_column(String(32), nullable=False)
    base_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    # 幂等键：同一剧本版本重复提交必须命中同一提案，不重复入账
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_continuity_proposal_idempotency"),
    )


Index(
    "ix_continuity_proposals_episode",
    ContinuityChangeProposal.episode_id,
    ContinuityChangeProposal.status,
)


class EpisodeContextManifest(Base):
    """上下文清单：保存实际使用的上下文内容与版本引用（不只存指针）。"""

    __tablename__ = "episode_context_manifests"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=ids.episode_context_manifest_id
    )
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    episode_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("series_episodes.id"), nullable=False
    )
    context_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    context_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "episode_id", "context_version", name="uq_context_manifest_episode_version"
        ),
    )


Index("ix_context_manifests_series_episode", EpisodeContextManifest.series_id, EpisodeContextManifest.episode_id)


class PlotThread(Base):
    """剧情线：续写时优先加载 open / in_progress 的条目。"""

    __tablename__ = "plot_threads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.plot_thread_id)
    series_id: Mapped[str] = mapped_column(String(32), ForeignKey("series.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    introduced_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_updated_episode_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    related_asset_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    related_event_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    resolution_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_plot_threads_series_status", PlotThread.series_id, PlotThread.status)
