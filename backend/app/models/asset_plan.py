"""资产定义表：解析阶段一次性定稿名称、占位位置、提示词与参考依赖。

设计要点（本表为新链路唯一权威来源，不兼容历史项目数据）：
- asset_key：资产唯一名称，一经生成不再变更；后续替换/重绘只改内容，不改名称与路径。
- media_path：固定占位路径，生成前即确定，成功产出后写入同名文件。
- prompt：生成提示词，分镜图/分镜视频的提示词须按模板引用已确认的参考资产名。
- status：pending（待生成）→ generated（已生成）→ reviewed/rejected（人工结论）。
- 依赖关系单独存于 AssetPlanDependency，按槽位记录参考资产名与位置。
"""
from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import ids
from app.models.base import Base, TimestampMixin


class AssetPlan(TimestampMixin, Base):
    """资产定义：一类资产一条定义，名称与路径固定不可变。"""

    __tablename__ = "asset_plan"
    __table_args__ = (
        UniqueConstraint("project_id", "asset_key", name="uq_asset_plan_project_key"),
        Index("ix_asset_plan_project_type_status", "project_id", "plan_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.asset_plan_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    # 定义类型：character/scene/prop/keyframe/clip（与 ASSET_TYPE 对齐但独立维护）
    plan_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 固定唯一名称：{类型前缀}_{语义片段}_{短哈希}，生成后永不修改
    asset_key: Mapped[str] = mapped_column(String(160), nullable=False)
    # 固定占位路径：data/media/{asset_key}.png|mp4，生成前即确定
    media_path: Mapped[str] = mapped_column(String(500), nullable=False)
    thumb_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 生成提示词：分镜图/分镜视频按模板写入参考资产名
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 状态：pending 待生成 / generated 已生成 / rejected 人工驳回
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    # 归属实体：角色/场景/道具对应项目级实体，分镜图/分镜视频对应镜头
    owner_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 生成成功后的正式资产记录与版本号
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # 人工确认标记：分镜图确认后方可分镜视频生成（D-13 闸口在定义层的映射）
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class AssetPlanDependency(TimestampMixin, Base):
    """资产定义依赖：记录一条定义依赖哪些参考资产名与位置，按槽位唯一。"""

    __tablename__ = "asset_plan_dependency"
    __table_args__ = (
        UniqueConstraint("plan_id", "slot", name="uq_asset_plan_dep_plan_slot"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.asset_plan_dep_id)
    plan_id: Mapped[str] = mapped_column(String(32), ForeignKey("asset_plan.id"), nullable=False, index=True)
    # 槽位：ref_identity/ref_costume/ref_scene/ref_prop/ref_image/ref_prev_frame
    slot: Mapped[str] = mapped_column(String(32), nullable=False)
    # 被依赖定义的 ID 与固定名称（名称冗余存储，便于校验断链）
    ref_plan_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_key: Mapped[str] = mapped_column(String(160), nullable=False)
    ref_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
