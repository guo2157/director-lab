"""跨集连续性模型：记录集间末帧传递链路。

设计约定（对应跨集连续性改造第三节）：
- 每集一条记录，追踪上一集末帧→当前集首帧的传递关系
- 状态机：pending → extracted → passed → failed
- 版本追踪：continuity_version 随上游重合成递增，旧版本自动失效
- 明确区分三类图片用途：角色参考图、场景参考图、跨集连续性首帧
"""
from datetime import datetime
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base
from app.models import ids


class EpisodeContinuity(Base):
    """跨集连续性记录：每集一条，追踪上一集末帧→当前集首帧的传递链路。

    字段说明：
    - episode: 当前集号
    - previous_episode: 上一集号（第 1 集为 None）
    - next_episode: 下一集号（最后一集为 None）
    - source_video_path: 上一集最终合成视频路径
    - source_composition_id: 上一集合成记录 ID
    - source_frame_path: 上一集末帧图片路径
    - source_frame_hash: 末帧 SHA256 校验值
    - source_frame_timestamp: 末帧在视频中的时间戳
    - source_video_duration: 上一集视频时长
    - source_video_fps: 上一集视频帧率
    - opening_frame_path: 当前集实际使用的首帧图片路径
    - opening_frame_asset_id: 当前集首帧对应的 Asset ID
    - continuity_status: pending(待处理) / extracted(已提取) / passed(已验证) / failed(失败)
    - continuity_version: 连续性资产版本（上游重合成时递增）
    - continuity_summary: 结构化跨集连续性摘要（场景/人物/道具/光照等）
    - verification_result: 首帧一致性验证结果
    - error_detail: 失败原因
    - fallback_used: 末帧提取是否使用了回退帧
    - fallback_reason: 回退原因
    """
    __tablename__ = "episode_continuity"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: ids.new_id("ect"))
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 上一集末帧来源
    source_composition_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_frame_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_frame_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_frame_timestamp: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_video_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_video_fps: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 当前集首帧使用
    opening_frame_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    opening_frame_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 状态与版本
    continuity_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    continuity_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # 连续性摘要与验证
    continuity_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # 异常信息
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(String(1), nullable=False, default="0", server_default="0")
    fallback_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_continuity_project_episode", EpisodeContinuity.__table__.c.project_id, EpisodeContinuity.__table__.c.episode)
Index("ix_continuity_project_status", EpisodeContinuity.__table__.c.project_id, EpisodeContinuity.__table__.c.continuity_status)
