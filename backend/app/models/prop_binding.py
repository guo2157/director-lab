"""角色-道具绑定关系模型（对应 PROP_BINDING_PLAN.md §2）。
设计要点：
- 绑定是关系数据，组合参考图是关系的附属视觉参考：组合图 Asset 的 asset_type
  使用 character_prop_binding 值但不进资产库一级分类，挂接在本表 anchor_asset_id
- 有效期以生命周期事件为边界（valid_from_event/valid_until_event），禁止永久固定；
  事件被 rejected 或 superseded 时关系不生效
- review_status 复用事件审核队列语义：pending/confirmed/rejected
"""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base
from app.models import ids


class CharacterPropBinding(Base):
    """角色-道具绑定关系：某角色在某剧情区间内以某种方式持有/关联某道具。

    - relation_type: carried(背负/携带) / held(手持) / worn(穿戴) / shared(多人共用)
    - anchor_asset_id: 组合参考图 Asset（asset_type=character_prop_binding），可空；
      无图时 Resolver 自动回退到独立参考图
    - valid_from_event / valid_until_event: 引用 prop_lifecycle_event 事件 ID，
      until 为空表示关系仍有效
    """

    __tablename__ = "character_prop_binding"
    __table_args__ = (
        Index("ix_cpb_char_prop", "character_id", "prop_id"),
        Index("ix_cpb_project_review", "project_id", "review_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: ids.new_id("cpb"))
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    character_id: Mapped[str] = mapped_column(String(32), ForeignKey("character.id"), nullable=False)
    prop_id: Mapped[str] = mapped_column(String(32), ForeignKey("prop.id"), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(16), nullable=False, default="carried", server_default="carried")
    anchor_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_from_event: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_until_event: Mapped[str | None] = mapped_column(String(32), nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="llm", server_default="llm")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
