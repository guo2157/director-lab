"""道具生命周期模型：事件驱动的道具连续性管理（对应 PROP_LIFECYCLE_PLAN.md §2）。
设计要点：
- 事件表只追加不覆盖：人工修正通过 supersedes_id 指向被修正事件，历史保留
- 分镜状态快照由事件推导，可重建；人工修改后 state_version 递增
- visibility 与 existence_state 分离：offscreen 不等于不存在，occluded 不等于销毁
- 通用性：不包含任何具体故事/道具名称的规则
"""
from datetime import datetime
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin
from app.models import ids


class PropLifecycleEvent(TimestampMixin, Base):
    """道具生命周期事件：只追加，不覆盖历史。

    - event_type: introduced/picked_up/held/carried/placed/transferred/dropped/moved/
      opened/closed/modified/damaged/repaired/consumed/destroyed/lost/recovered
    - source_type: script（剧本原文）/ llm_inference（模型推断）/ human（人工）
    - source_evidence: 剧本原文证据，source_type=script 时必填
    - review_status: pending/confirmed/rejected；llm_inference 默认 pending，
      确认后才参与状态推导
    - supersedes_id: 人工修正时指向被修正事件 ID，被修正事件保留不删
    """

    __tablename__ = "prop_lifecycle_event"
    __table_args__ = (
        Index("ix_ple_project_prop_ep_shot", "project_id", "prop_id", "episode", "shot_id"),
        Index("ix_ple_project_review", "project_id", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: ids.new_id("plc"))
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    prop_id: Mapped[str] = mapped_column(String(32), ForeignKey("prop.id"), nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    holder_before: Mapped[str | None] = mapped_column(String(32), nullable=True)
    holder_after: Mapped[str | None] = mapped_column(String(32), nullable=True)
    location_before: Mapped[str | None] = mapped_column(String(200), nullable=True)
    location_after: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 属性化物理状态：{condition, content, ...} 可扩展，不用固定枚举限制
    physical_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    physical_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    supersedes_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class PropShotState(TimestampMixin, Base):
    """分镜道具状态快照：由确认事件按序推导（Prop Lifecycle Engine 唯一写入方）。

    - existence_state: not_introduced/active/destroyed/consumed/lost/unknown
    - interaction_state: unassigned/held/carried/placed/transferred
    - visibility: visible/offscreen/occluded/unknown（分镜分析产出）
    - reference_required: visible→true；offscreen/occluded→false；不存在→false 且禁止生成
    - continuity_from_shot / continuity_event_ids: 状态继承链路可追溯
    """

    __tablename__ = "prop_shot_state"
    __table_args__ = (
        UniqueConstraint("project_id", "prop_id", "shot_id", name="uq_pss_project_prop_shot"),
        Index("ix_pss_project_shot", "project_id", "shot_id"),
        # 仅按 shot_id 反查（reference_resolver / prop_validator / prompt_qc 高频路径）：
        # 现有复合索引以 project_id 打头，单 shot_id 查询无法使用，原为全表扫描。
        Index("ix_pss_shot_id", "shot_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: ids.new_id("pss"))
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    prop_id: Mapped[str] = mapped_column(String(32), ForeignKey("prop.id"), nullable=False)
    shot_id: Mapped[str] = mapped_column(String(32), ForeignKey("shot.id"), nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    existence_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", server_default="unknown")
    interaction_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unassigned", server_default="unassigned")
    holder_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[str | None] = mapped_column(String(200), nullable=True)
    physical_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", server_default="unknown")
    reference_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    appearance_constraint: Mapped[str | None] = mapped_column(String(200), nullable=True)
    continuity_from_shot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    continuity_event_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class PropEpisodeSnapshot(TimestampMixin, Base):
    """集末道具状态快照：跨集连续性依据（不依赖上一集末帧图片）。

    - final_state: 该集最后一个镜头的 PropShotState 内容快照
    - 下一集状态推导时读取本表作为初始状态
    """

    __tablename__ = "prop_episode_snapshot"
    __table_args__ = (
        UniqueConstraint("project_id", "prop_id", "episode", name="uq_pes_project_prop_episode"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: ids.new_id("pes"))
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    prop_id: Mapped[str] = mapped_column(String(32), ForeignKey("prop.id"), nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    final_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analysis_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
