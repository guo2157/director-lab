"""音频模块数据模型（音频模块与 H3 VA 工作流改造，2026-11-06）。

设计约束（docs/agent/audio-module-audit-report-20261106.md）：
- 复用 Asset（asset_type=audio）与 Job（generate_audio/generate_bgm/generate_sfx），不重复建表
- 生成状态与审核状态分开存储；审核记录绑定具体 asset_id + version
- 时间统一毫秒（start_time_ms / duration_ms）
- 旁白/BGM 可跨镜头：shot_id 可空 + cross_shot 标记
- H3 原生音频作为独立轨道管理（track_type=native_audio），不覆盖对白轨道
"""
from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models import ids


class VoiceProfile(TimestampMixin, Base):
    """角色音色配置：同一角色跨分镜跨集默认复用同一配置。

    - 单条对白可在 AudioSegment 上覆盖 speed/emotion，覆盖只作用于该片段
    - 修改默认音色不静默覆盖已生成并审核通过的历史音频（历史资产版本化保留）
    - is_default 唯一性由应用层保证（同 character 下仅一条 default）
    """
    __tablename__ = "voice_profile"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.voice_profile_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    character_id: Mapped[str] = mapped_column(String(32), ForeignKey("character.id"), nullable=False, index=True)
    # TTS 服务提供方（如 qwen3_tts）
    voice_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="qwen3_tts")
    # 音色模型（Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice）
    voice_model: Mapped[str] = mapped_column(String(128), nullable=False, default="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
    # TTS 模式（V1 仅 custom_voice；未来可扩展 base，不影响数据结构）
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="custom_voice")
    # speaker：固定音色（如 Vivian / Serena），必须来自模型支持列表，禁止 LLM 编造
    voice_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 展示名称
    voice_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 语言（默认 Chinese）
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="Chinese")
    # 默认 instruct（情绪语气指令，如『自然、清晰、成熟』）
    default_instruct: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 默认语速（1.0 为原速；TTS 不可控时仅作记录）
    speed: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    # 音调（服务商支持时生效）
    pitch: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 默认情绪（SHOT_EMOTION 枚举，映射到 TTS instruct 指令）
    default_emotion: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    # 可选音色参考文件资产 ID（voice_clone 模式，V1 不使用）
    voice_reference_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 是否启用（禁用后不可用于新生成，历史音频不受影响）
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 是否人工确认（speaker 描述需试听确认，未确认前提示）
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 是否角色默认音色（同角色仅一条）
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 试听缓存：已生成试听的音频资产 ID（asset_type=audio），命中时无需再次调用 ComfyUI
    preview_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 试听缓存：生成该音频时使用的文本，文本变化即视为缓存失效
    preview_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 试听缓存：speaker+语言+instruct+文本 的指纹，任一项变化即视为缓存失效
    preview_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ===== 角色音色智能匹配扩展（2026-11-07）=====
    # 系统音色库 ID（VoiceLibrary.voice_code 或 id，绑定真实存在的音色）
    voice_library_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 程序计算的匹配评分（0-100，LLM 不决定分数）
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 匹配原因说明
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 其他候选音色（JSON 数组：[{voice_library_id, speaker_id, match_score, match_reason}]）
    alternatives: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 是否人工锁定（锁定后自动匹配不可覆盖）
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 绑定来源：ai（自动匹配）/ manual（人工指定）/ inherited（续集继承）
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # 角色声音画像（LLM 产出的结构化 JSON：gender/age/pitch/timbre/personality/style 等）
    voice_profile_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


Index("ix_voice_profile_character", VoiceProfile.__table__.c.character_id, VoiceProfile.__table__.c.is_default)


class AudioSegment(TimestampMixin, Base):
    """音频时间轴片段：对白/旁白/BGM/SFX/环境声/H3 原生音频在时间线上的具体片段。

    - 剧本解析阶段生成（对白/旁白/BGM/SFX/环境声规划）
    - H3 原生音频片段由 clip 生成后检测登记（track_type=native_audio，绑定视频资产版本）
    - audio_asset_id 指向当前选中的 Asset 版本；重新生成创建新版本，旧版本保留
    - generation_status 与 review_status 分开存储
    """
    __tablename__ = "audio_segment"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.audio_segment_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    episode: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 所属镜头（对白/SFX 通常绑定镜头；旁白/BGM/环境声可跨镜头，可空）
    shot_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("shot.id"), nullable=True, index=True)
    # 轨道类型：dialogue / narration / bgm / sfx / ambient / native_audio
    track_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 片段细分类型：character_dialogue / voiceover / music / effect / ambience / h3_native
    segment_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # 说话角色（对白必填）
    character_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("character.id"), nullable=True)
    # 文本内容（对白原文/旁白原文/音效描述/音乐描述）
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 使用的音色配置（对白/旁白）
    voice_profile_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("voice_profile.id"), nullable=True)
    # 片段级情绪覆盖（仅作用于本片段）
    emotion: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 片段级 instruct 覆盖（情绪语气指令，如『愤怒、压迫感强，语速稍快』）
    instruct: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 片段级语速覆盖（仅作用于本片段）
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 对白顺序（同分镜内多角色对白按 sequence 排序生成，不合并）
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 时间轴起始位置（毫秒，相对本集时间线）
    start_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 时长（毫秒；生成后以媒体探测为准回写）
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 当前选中的音频资产版本
    audio_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 关联的视频资产（native_audio 专用：绑定具体视频版本，视频重生成后重新检测）
    video_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 生成状态：pending / queued / processing / completed / failed / cancelled
    generation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # 审核状态：not_required / pending / approved / rejected
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # 跨镜头标记（旁白/BGM/环境声跨分镜连续）
    cross_shot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 片段版本（重新规划时递增）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 复用的依赖指纹（A4）：生成该音频时固化的台词/音色/工作流版本/参数摘要。
    # 历史片段为空 → 读取端按「不可复用」处理，不静默当作可复用。
    tts_fingerprint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 备注（规划建议、冲突说明等）
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_audio_segment_project_episode_track", AudioSegment.__table__.c.project_id,
      AudioSegment.__table__.c.episode, AudioSegment.__table__.c.track_type)
Index("ix_audio_segment_shot", AudioSegment.__table__.c.shot_id)


class VoiceLibrary(TimestampMixin, Base):
    """系统默认音色库（全局级，不隶属项目；角色音色智能匹配，2026-11-07）。

    - speaker_id 必须来自实际部署的 TTS workflow meta（voice_values），禁止虚构
    - 描述类字段（听感年龄/音调/音色特点/性格标签/说话风格）从 workflow meta
      voice_descriptions 推导初始化，缺项留空，不凭空填写
    - 关闭音色后新匹配不可选用；历史绑定不受影响
    """
    __tablename__ = "voice_library"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.voice_library_id)
    # 系统音色编码（如 male_a / female_f，稳定标识，跨部署不变）
    voice_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 显示名称（如 男性音色 A）
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Qwen3-TTS 实际 speaker_id（来自 workflow meta voice_values）
    speaker_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # workflow meta 展示名（如 Vivian → 薇薇安）
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 性别呈现：male / female
    gender_presentation: Mapped[str] = mapped_column(String(16), nullable=False)
    # 听感年龄（young / mature / child / unknown）
    perceived_age: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    # 音调（high / medium_high / medium / medium_low / low / unknown）
    pitch: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    # 音色特征标签（JSON 数组，如 ["bright", "clear"]）
    timbre: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 性格标签（JSON 数组）
    personality_tags: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 说话风格（JSON 数组）
    speaking_styles: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 语言（中文/英文/日语/韩语等）
    language: Mapped[str] = mapped_column(String(64), nullable=False, default="中文")
    # workflow meta 原始描述
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 音源 workflow 标识（数据溯源：wf_tts_qwen3_custom_voice）
    source_workflow: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 试听音频 Asset（asset_type=audio），缺失时通过 TTS 生成并缓存
    preview_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 是否启用（禁用后新匹配不可选，历史绑定不受影响）
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 排序（男 1-5，女 6-9）
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 其他元数据
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


Index("ix_voice_library_gender_enabled", VoiceLibrary.__table__.c.gender_presentation,
      VoiceLibrary.__table__.c.enabled)


class AudioReview(TimestampMixin, Base):
    """音频版本审核记录：绑定具体 audio_asset_id + 版本，不继承到新版本。

    - 生成成功不等于审核通过；重新生成后新版本回到 pending
    - native_audio 审核决定启用/静音策略
    """
    __tablename__ = "audio_review"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.audio_review_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    # 审核对象：具体音频资产版本
    audio_asset_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    asset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 关联片段（可空：资产级审核）
    segment_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("audio_segment.id"), nullable=True)
    # 审核结论：approved / rejected
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # 审核意见（驳回原因等）
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审核人
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
