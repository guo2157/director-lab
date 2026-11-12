"""分镜视频重新生成工作台结构化配置模型。

设计约定（对应多参考图视频重新生成工作台整改方案）：
- 一份配置对应一个镜头的一个产物目标（keyframe / clip），可反复编辑与保存
- 结构化参考图绑定是唯一事实来源，提示词只是绑定关系的自然语言表达
- 槽位 node_id 一律由工作流定义中的标记解析得到，本表不做任何推断
- 空间布局、动作时间轴、道具绑定与参考图绑定分别独立存储：
  身份绑定解决『谁是谁』，空间布局解决『谁在哪里』，动作绑定解决『谁做什么』
- 生成时冻结进 Job.input_snapshot；本表只保存可编辑的草稿/工作副本
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models import ids


class StoryboardRegenConfig(Base):
    """分镜重新生成工作台配置。

    字段说明：
    - shot_id / target：一个镜头一个产物目标一份配置（分镜图或分镜视频）
    - workflow_id / workflow_version：当前绑定的工作流与版本，用于校验槽位是否仍匹配
    - version：配置版本号，每次保存递增，便于对比与追溯
    - references：结构化参考图绑定列表
      [{reference_id, slot_id, node_id, reference_type, asset_type, asset_id,
        asset_name, character_id, image_url, binding_description,
        identity_constraints, required, enabled, sort_order}]
    - character_bindings：角色身份绑定
      [{character_id, character_name, identity_reference_id, identity_slot_id,
        identity_constraints}]
    - spatial_layout：角色空间布局（初始/最终位置、朝向、景深、运动路径）
    - action_timeline：动作时间轴（动作主体、起止进度、动作内容）
    - prop_bindings：道具绑定（持有者、可见性、是否随角色移动、是否交接）
    - prompt_sections：分层提示词各段系统生成结果与用户自定义正文
    - final_prompt：用户最终确认的完整视频提示词
    - parameters：采样器、尺寸、帧数等生成参数
    - extra：连续性检查结论等扩展数据
    """

    __tablename__ = "storyboard_regen_config"
    __table_args__ = (
        UniqueConstraint("shot_id", "target", name="uq_regen_config_shot_target"),
        Index("ix_regen_config_project_target", "project_id", "target"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: ids.new_id("rgc"))
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    shot_id: Mapped[str] = mapped_column(String(32), ForeignKey("shot.id"), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(16), nullable=False, default="clip", server_default="clip")
    workflow_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    references: Mapped[list | None] = mapped_column(JSON, nullable=True)
    character_bindings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    spatial_layout: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    action_timeline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    prop_bindings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    prompt_sections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    final_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
