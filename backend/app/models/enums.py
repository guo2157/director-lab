"""枚举定义：全部取值的唯一权威来源（应用层）。

权威文档：ARCHITECTURE.md §3（四套状态机）、DATABASE.md §2（业务枚举）。
纪律（AGENTS.md §3-6）：asset_type / workflow_type / job_type 三套枚举语义独立，
同名取值（如 clip）不得交叉赋值。
"""


def _values(*items: str) -> tuple[str, ...]:
    return tuple(items)


# ============ 四套状态机（ARCHITECTURE.md §3，必须分别保存，不得互相替代） ============

# 3.1 项目生命周期状态机
PROJECT_STATUS = _values("draft", "active", "archived")

# 3.2 镜头制作状态机（pending 可附带 blocked_reason 子状态，仍属 pending）
SHOT_STATUS = _values("pending", "generating", "review", "done", "redo")

# 3.3 任务状态机（succeeded/failed/lost/cancelled 为终态）
JOB_STATUS = _values("queued", "running", "submission_unknown", "succeeded", "failed", "lost", "cancelled")
JOB_TERMINAL_STATUS = _values("succeeded", "failed", "lost", "cancelled")

# 3.4 产物有效性状态机
ASSET_VALIDITY = _values("current", "stale", "superseded", "archived")

# ============ 任务提交对账（DATABASE.md job 表） ============
SUBMISSION_STATE = _values("not_submitted", "submitting", "submitted", "submission_unknown")

# ============ 三套业务枚举（禁止混用） ============
ASSET_TYPE = _values("character", "scene", "prop", "keyframe", "clip", "audio", "last_frame")
WORKFLOW_TYPE = _values("image", "video", "tts", "bgm", "sfx")
JOB_TYPE = _values(
    "parse", "generate_keyframe", "generate_clip", "generate_audio",
    "generate_bgm", "generate_sfx", "compose", "export", "series_agent",
)

# ============ 镜头字段枚举（DATABASE.md shot 表） ============
SHOT_KIND = _values("dialogue", "narration", "visual", "transition")
SHOT_EMOTION = _values("neutral", "happy", "sad", "angry", "fearful")
AUDIO_SOURCE = _values("h3_va", "tts_fallback", "none")
CHECK_STATUS = _values("passed", "review", "failed", "not_applicable")

# ============ 项目字段枚举 ============
CREATION_TYPE = _values("novel", "script")
ASPECT_RATIO = _values("9:16", "16:9", "1:1")

# ============ 解析版本状态 ============
PARSE_VERSION_STATUS = _values("candidate", "current", "superseded", "failed")

# ============ 公共资产人工审核状态（D-23：资产库 → 分镜图 的必经闸口） ============
ASSETS_REVIEW_STATUS = _values("pending", "approved", "rejected")

# ============ 资产引用角色（asset_ref.role） ============
ASSET_REF_ROLE = _values(
    "identity", "costume", "scene", "prop", "reference",
    "first_frame", "end_frame", "prev_frame", "dialogue", "sfx", "bgm",
)

# ============ 依赖有效性（dependency_ref.validity） ============
DEPENDENCY_VALIDITY = _values("current", "stale")

# ============ 合成与导出 ============
COMPOSITION_STATUS = _values("composing", "succeeded", "failed")
EXPORT_STATUS = _values("exporting", "succeeded", "failed")
EXPORT_FORMAT = _values("mp4",)
DELIVERY_PROFILE = _values("standard", "high_quality")
# ============ 音频模块（音频模块与 H3 VA 工作流改造，2026-11-06） ============
# 音频轨道类型（audio_segment.track_type）：六轨独立控制
TRACK_TYPE = _values("dialogue", "narration", "bgm", "sfx", "ambient", "native_audio")
# 片段细分类型（audio_segment.segment_type）
SEGMENT_TYPE = _values(
    "character_dialogue", "voiceover", "music", "effect", "ambience", "h3_native",
)
# 音频片段生成状态（与 JOB_STATUS 分开：片段级状态，pending 为规划未生成）
# waiting_voice_confirmation：角色音色尚未确认，对白片段暂缓生成（音色确认调度闸口，2026-11-07）
AUDIO_GENERATION_STATUS = _values(
    "pending", "waiting_voice_confirmation", "queued", "processing", "completed", "failed", "cancelled",
)
# 音频审核状态（生成成功不等于审核通过，分开存储）
AUDIO_REVIEW_STATUS = _values("not_required", "pending", "approved", "rejected")
# H3 原生音频策略（先检测再决定，不允许 LLM 宣称）
NATIVE_AUDIO_POLICY = _values("pending_detection", "keep", "mute", "manual_confirm")
