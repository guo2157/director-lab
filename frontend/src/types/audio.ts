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
  | 'waiting_voice_confirmation'
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
export type NativeAudioPolicy = 'discard' | 'preserve' | 'preserve_ambience_only' | 'native'

/** 项目级音频策略（Project.settings.audio_config） */
export interface AudioConfig {
  tts_enabled: boolean
  tts_provider: string
  tts_model: string
  /** 默认 native：直接使用 H3 内嵌音轨（口型同步），TTS 仅作 ref_audio 音色参考 */
  native_audio_policy: NativeAudioPolicy
  bgm_enabled: boolean
  sfx_enabled: boolean
}

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
  waiting_voice_confirmation: '待确认音色',
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

// ============ 角色音色智能匹配（2026-11-07）============

/** 系统默认音色（VoiceLibrary，speaker_id 溯源 workflow meta） */
export interface SystemVoice {
  id: string
  voice_code: string
  name: string
  speaker_id: string
  display_name: string
  gender_presentation: 'male' | 'female'
  perceived_age: string
  pitch: string
  timbre: string[]
  personality_tags: string[]
  speaking_styles: string[]
  language: string
  description: string
  source_workflow: string
  enabled: boolean
  sort_order: number
  has_preview: boolean
}

/** 系统音色库列表响应 */
export interface SystemVoiceList {
  items: SystemVoice[]
  total: number
  male_count: number
  female_count: number
}

/** 角色音色绑定详情（含匹配结果与候选） */
export interface VoiceBinding {
  voice_profile_id: string
  voice_library_id: string | null
  voice_id: string
  voice_name: string
  match_score: number | null
  match_reason: string | null
  alternatives: Array<{
    voice_library_id: string
    voice_code: string
    name: string
    speaker_id: string
    match_score: number
    match_reason: string
  }>
  voice_profile: Record<string, unknown>
  status: 'locked' | 'confirmed' | 'pending'
  is_manual: boolean
  source: 'ai' | 'manual' | 'inherited'
  approved: boolean
  is_locked: boolean
}

/** 角色音色绑定响应 */
export interface VoiceBindingResponse {
  character_id: string
  character_name: string
  binding: VoiceBinding | null
}

/** 批量匹配结果条目 */
export interface VoiceMatchItem {
  character_id: string
  character_name: string
  voice_code: string
  voice_name: string
  speaker_id: string
  match_score: number
  match_reason: string
  reused: boolean
  alternatives: VoiceBinding['alternatives']
}

/** 批量匹配响应 */
export interface VoiceMatchResult {
  matched: VoiceMatchItem[]
  skipped: Array<{ character_id: string; name: string; reason: string }>
  total: number
}

/** 音频生产进度（按轨道分组的状态统计） */
export interface AudioProductionProgress {
  project_id: string
  progress: Record<'dialogue' | 'narration' | 'bgm' | 'sfx', { total: number } & Record<string, number>>
}

/** 音频片段明细条目（导演台展开视图） */
export interface AudioSegmentDetail {
  id: string
  episode: number
  shot_id: string | null
  track_type: AudioTrackType
  segment_type: AudioSegmentType
  character_id: string | null
  character_name: string | null
  text: string | null
  voice_profile_id: string | null
  sequence: number | null
  generation_status: AudioGenerationStatus
  review_status: AudioReviewStatus
  audio_asset_id: string | null
  duration_ms: number | null
  note: string | null
}

/** 音频片段明细分页响应 */
export interface AudioSegmentDetailPage {
  items: AudioSegmentDetail[]
  total: number
  page: number
  page_size: number
}
