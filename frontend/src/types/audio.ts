/** 音频模块类型（音频模块与 H3 VA 工作流改造，2026-11-06） */

/** 音频轨道类型：六轨独立控制 */
export type AudioTrackType = 'dialogue' | 'narration' | 'bgm' | 'sfx' | 'ambient' | 'native_audio'

/** 片段细分类型 */
export type AudioSegmentType =
  | 'character_dialogue'
  | 'voiceover'
  | 'music'
  | 'effect'
  | 'ambience'
  | 'h3_native'

/** 片段生成状态（与审核状态分开） */
export type AudioGenerationStatus =
  | 'pending'
  | 'queued'
  | 'processing'
  | 'completed'
  | 'failed'
  | 'cancelled'

/** 片段审核状态 */
export type AudioReviewStatus = 'not_required' | 'pending' | 'approved' | 'rejected'

/** 音频片段（时间轴片段） */
export interface AudioSegment {
  id: string
  project_id: string
  episode: number
  shot_id: string | null
  track_type: AudioTrackType
  segment_type: AudioSegmentType
  character_id: string | null
  text: string | null
  voice_profile_id: string | null
  emotion: string | null
  /** 片段级 instruct 覆盖（情绪语气指令） */
  instruct: string | null
  /** 对白顺序（同分镜内多角色对白按序生成） */
  sequence: number | null
  speed: number | null
  /** 时间轴起始位置（毫秒） */
  start_time_ms: number | null
  /** 时长（毫秒，生成后以媒体探测为准） */
  duration_ms: number | null
  audio_asset_id: string | null
  video_asset_id: string | null
  generation_status: AudioGenerationStatus
  review_status: AudioReviewStatus
  cross_shot: boolean
  version: number
  note: string | null
}

/** 角色音色配置（V1 统一 Qwen3-TTS CustomVoice） */
export interface VoiceProfile {
  id: string
  project_id: string
  character_id: string
  voice_provider: string
  voice_model: string
  /** TTS 模式（V1 仅 custom_voice） */
  mode: string
  /** speaker：固定音色，来自模型支持列表 */
  voice_id: string
  voice_name: string
  language: string
  default_instruct: string
  speed: number
  pitch: number | null
  default_emotion: string
  voice_reference_asset_id: string | null
  enabled: boolean
  approved: boolean
  is_default: boolean
}

/** speaker 配置（来自后端 workflow meta，禁止前端硬编码） */
export interface SpeakerInfo {
  speaker: string
  display_name: string
  language: string[]
  description: string
  /** 性别标注：male / female；旧工作流未配置时为空字符串 */
  gender?: string
  enabled: boolean
}

/** 项目级旁白 speaker 配置 */
export interface NarratorVoice {
  provider: string
  model: string
  speaker: string
  language: string
  default_instruct: string
}

/** H3 原生音轨处理策略（项目级） */
export type NativeAudioPolicy = 'discard' | 'preserve' | 'preserve_ambience_only'

/** 项目级音频策略（Project.settings.audio_config） */
export interface AudioConfig {
  tts_enabled: boolean
  tts_provider: string
  tts_model: string
  /** V1 默认 discard：丢弃 H3 原生音轨，最终用 TTS/BGM/SFX 重建音轨 */
  native_audio_policy: NativeAudioPolicy
  bgm_enabled: boolean
  sfx_enabled: boolean
}

/** 原生音轨策略显示名与说明 */
export const NATIVE_AUDIO_POLICY_OPTIONS: Array<{
  value: NativeAudioPolicy
  label: string
  description: string
}> = [
  {
    value: 'discard',
    label: '丢弃（默认）',
    description: '丢弃 H3 原生音轨，最终音轨由 TTS 对白/旁白、BGM、SFX 重建',
  },
  {
    value: 'preserve',
    label: '保留',
    description: '保留 H3 原生音轨，需注意与 TTS 对白可能重复，须人工确认',
  },
  {
    value: 'preserve_ambience_only',
    label: '仅保留环境声',
    description: '需可靠人声分离能力与审核；当前按丢弃处理，分离能力就绪后启用',
  },
]

/** 音频规划（按轨道分组） */
export interface AudioPlan {
  project_id: string
  episode: number
  tracks: Record<string, AudioSegment[]>
  counts: Record<string, number>
  total: number
}

/** 规划生成结果统计 */
export interface AudioPlanBuildResult {
  episode: number
  dialogue: number
  narration: number
  bgm: number
  sfx: number
  ambient: number
  total: number
  note?: string
}

/** 音频时间轴 */
export interface AudioTimeline {
  project_id: string
  episode: number
  items: AudioSegment[]
  total: number
}

/** 轨道显示名映射 */
export const TRACK_TYPE_LABELS: Record<AudioTrackType, string> = {
  dialogue: '角色对白',
  narration: '旁白',
  bgm: 'BGM',
  sfx: 'SFX',
  ambient: '环境声',
  native_audio: 'H3 原生音频',
}

/** 生成状态显示名映射 */
export const GENERATION_STATUS_LABELS: Record<AudioGenerationStatus, string> = {
  pending: '待生成',
  queued: '排队中',
  processing: '生成中',
  completed: '已生成',
  failed: '失败',
  cancelled: '已取消',
}

/** 审核状态显示名映射 */
export const REVIEW_STATUS_LABELS: Record<AudioReviewStatus, string> = {
  not_required: '无需审核',
  pending: '待审核',
  approved: '已通过',
  rejected: '已驳回',
}
