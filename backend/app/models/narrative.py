"""核心叙事实体：剧本版本、剧本评估、项目、角色、场景、场景-集关联、道具、镜头。

权威定义：DATABASE.md §2 核心表（逐字段落位）。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models import ids


class ScriptVersion(Base):
    """剧本版本：不可变快照。"""

    __tablename__ = "script_version"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_script_version_project_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.script_version_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ScriptEstimate(Base):
    """剧本评估。"""

    __tablename__ = "script_estimate"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.script_estimate_id)
    script_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("script_version.id"), nullable=False, index=True)
    algorithm_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 确认时锁定的总集数快照：确认后不可修改，保证多轮解析口径一致
    total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Project(TimestampMixin, Base):
    """项目。"""

    __tablename__ = "project"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.project_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    creation_type: Mapped[str] = mapped_column(String(16), nullable=False, default="novel", server_default="novel")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    aspect_ratio: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default="draft")
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    episode_plan: Mapped[list | None] = mapped_column(JSON, nullable=True)
    estimated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    estimated_text_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_script_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    current_parse_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 公共资产（角色定妆图/场景基准图/道具锚定图）人工审核状态（D-23）：
    # pending=待审核 / approved=审核通过（放行分镜图） / rejected=审核驳回（需返工）
    assets_review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    assets_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Character(TimestampMixin, Base):
    """角色（项目级资产层）。"""

    __tablename__ = "character"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_character_project_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.character_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 归一化名（去括号别名/空白/小写）：跨集跨轮解析近名去重匹配用
    normalized_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    traits: Mapped[str | None] = mapped_column(Text, nullable=True)
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    portrait_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 别名列表（来自名称括号即“张三（张三丰）”的括号别名）：JSON 数组，主名不重复收录
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 角色类型：individual（个体）/ crowd（群体，如“众人”“村民们”“侍卫们”）
    character_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class Scene(TimestampMixin, Base):
    """场景（项目级资产层，跨集公用）。"""

    __tablename__ = "scene"
    __table_args__ = (UniqueConstraint("project_id", "seq", name="uq_scene_project_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.scene_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    episode: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # 归一化名（地点+时段复合键，保留括号区分子场景）：场景去重匹配用
    normalized_name: Mapped[str | None] = mapped_column(String(220), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    baseline_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 场景板（plate）：LLM 编译器产出的空镜环境规格（P1）
    # plate 存完整 JSON（location_id/anchors/removed 审计账本等），plate_text 存四行文本
    # 不覆盖 description：分镜层仍需要 beat（谁做了什么）
    plate: Mapped[str | None] = mapped_column(Text, nullable=True)
    plate_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class SceneEpisode(Base):
    """场景-集关联：同一地点多集出现只保留一条 scene 记录。"""

    __tablename__ = "scene_episode"
    __table_args__ = (UniqueConstraint("scene_id", "episode", name="uq_scene_episode_scene_episode"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.scene_episode_id)
    scene_id: Mapped[str] = mapped_column(String(32), ForeignKey("scene.id"), nullable=False, index=True)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    seq_in_episode: Mapped[int] = mapped_column(Integer, nullable=False)
    # 集级独立描述：保留该集对该场景的具体描述，不覆盖项目级 scene.description
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Prop(TimestampMixin, Base):
    """道具（项目级资产层）。"""

    __tablename__ = "prop"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_prop_project_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.prop_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 归一化名（去括号别名/空白/小写）：跨集跨轮解析近名去重匹配用
    normalized_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_key_prop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    anchor_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    anchor_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 道具分类：handheld/carried/scene_fixed/shared/key_prop/background（生命周期管理用，通用枚举）
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 结构化外观属性：{material, color, shape, size, unique_features, ...} 与自由文本 appearance 并存
    appearance_attrs: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Shot(TimestampMixin, Base):
    """镜头（分镜层，集级独立）。"""

    __tablename__ = "shot"
    __table_args__ = (UniqueConstraint("scene_id", "episode", "seq", name="uq_shot_scene_episode_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.shot_id)
    scene_id: Mapped[str] = mapped_column(String(32), ForeignKey("scene.id"), nullable=False, index=True)
    episode: Mapped[int] = mapped_column(Integer, nullable=False)
    character_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("character.id"), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    dialogue: Mapped[str | None] = mapped_column(Text, nullable=True)
    dialogue_segments: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 本镜涉及道具名数组（剧本解析 LLM 输出，落库保留；生命周期状态推导的输入之一）
    props: Mapped[list | None] = mapped_column(JSON, nullable=True)
    shot_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    shot_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    camera_angle: Mapped[str | None] = mapped_column(String(32), nullable=True)
    camera_movement: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transition: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shot_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotion: Mapped[str | None] = mapped_column(String(16), nullable=True)
    soundscape: Mapped[str | None] = mapped_column(Text, nullable=True)
    bgm: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    # 静图先行闸口（D-13）：分镜图人工确认标记；重绘/上游失效时回退 False
    keyframe_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    actual_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_actual_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    audio_check_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dialogue_check_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    av_sync_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_jump: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    requires_prev_frame: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    start_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    end_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_frame_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    beats: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # --- 聚合改造新增（见 director_lab_full_pipeline_refactor_execution_plan.md §3.3）---
    # timeline：镜内时间轴，成为唯一事实源；beats 降级为派生只读视图
    timeline: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # cut_reason：本镜结束原因（枚举见方案 §3.2）
    cut_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # aggregation_meta：预算占用与聚合决策记录
    aggregation_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    emotion_curve: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # --- 镜头级契约（shot_plan v1，分镜图生成改造 Phase 1）---
    # 分镜规划的导演决策落库：画内角色 / 画外声音 / 可见道具 / 构图 / 首帧状态。
    # 下游 compiler / resolver / 门禁 / 注入器统一读这一份出场清单（ShotCast），
    # 不再各写一套启发式推断「谁在画面里」。无该字段的镜头走旧启发式兜底，行为不变。
    shot_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# shot 表建议索引：(scene_id, episode) 支撑分镜看板按场景按集加载
from sqlalchemy import Index  # noqa: E402

Index("ix_shot_scene_episode", Shot.__table__.c.scene_id, Shot.__table__.c.episode)
# 按集取镜头（ordered_episode_shots / audio_plan / continuity 等高频路径）：
# filter(Shot.episode).order_by(...) 原为全表扫描。
Index("ix_shot_episode", Shot.__table__.c.episode)
# 按状态筛选镜头（合成/审核：filter(status.in_([...]))）。
Index("ix_shot_status", Shot.__table__.c.status)
# 场景内镜头排序：filter(scene_id).order_by(seq) 原需临时 B-Tree。
Index("ix_shot_scene_seq", Shot.__table__.c.scene_id, Shot.__table__.c.seq)
# 集内场景播放顺序：filter(episode).order_by(seq_in_episode) 原为全表扫描 + 临时排序。
Index("ix_scene_episode_episode_seq", SceneEpisode.__table__.c.episode, SceneEpisode.__table__.c.seq_in_episode)


class EpisodeParseRecord(Base):
    """集级解析记录：按『集 × 资产类型』双粒度跟踪解析状态。

    唯一键 (project_id, episode_no, asset_type)；Job 仍是任务入口，本表为集级明细。
    状态机：pending → running → succeeded/failed；failed 可重试回 running。
    """

    __tablename__ = "episode_parse_record"
    __table_args__ = (
        UniqueConstraint("project_id", "episode_no", "asset_type", name="uq_epr_project_episode_type"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.episode_parse_record_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    episode_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 资产类型：characters/scenes/props/shots（对应解析输出的实体分组）
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 乐观锁版本：并发提交终态校验用
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    asset_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
