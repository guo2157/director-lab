import { apiUrl, get, post, patch, put } from './client'
import type {
  AudioConfig,
  AudioPlan,
  AudioPlanBuildResult,
  AudioTimeline,
  NarratorVoice,
  SpeakerInfo,
  VoiceProfile,
} from '@/types/audio'

/** 生成/重建音频规划（幂等：清旧重建，native_audio 片段保留） */
export function buildAudioPlan(projectId: string, episode: number) {
  return post<AudioPlanBuildResult>(`/audio/projects/${projectId}/episodes/${episode}/plan`)
}

/** 获取音频规划（按轨道分组 + 统计） */
export function getAudioPlan(projectId: string, episode: number) {
  return get<AudioPlan>(`/audio/projects/${projectId}/episodes/${episode}/plan`)
}

/** 获取音频时间轴（最终合成页轨道配置数据源） */
export function getAudioTimeline(projectId: string, episode: number) {
  return get<AudioTimeline>(`/audio/projects/${projectId}/episodes/${episode}/timeline`)
}

/** 重新生成片段（创建 generate_audio/generate_bgm/generate_sfx 任务，新版本不覆盖旧资产） */
export function regenerateAudioSegment(
  segmentId: string,
  payload?: { emotion?: string; instruct?: string; duration?: number },
) {
  return post<{ job_id: string; segment_id: string; job_type: string }>(
    `/audio/segments/${segmentId}/regenerate`,
    payload ?? {},
  )
}

/** 上传替换音频（登记新版本资产，片段指向新资产） */
export function replaceAudioSegment(segmentId: string, filePath: string) {
  return post<{ asset_id: string; version: number; segment_id: string }>(
    `/audio/segments/${segmentId}/replace`,
    { file_path: filePath },
  )
}

/** 审核具体音频资产版本（approved / rejected） */
export function reviewAudioAsset(
  assetId: string,
  payload: { status: 'approved' | 'rejected'; comment?: string; segment_id?: string; reviewed_by?: string },
) {
  return post<{ review_id: string; asset_id: string; status: string }>(
    `/audio/assets/${assetId}/review`,
    payload,
  )
}

/** 列出角色音色配置 */
export function listCharacterVoices(characterId: string) {
  return get<{ character_id: string; voices: VoiceProfile[] }>(
    `/audio/characters/${characterId}/voices`,
  )
}

/** 新增角色音色配置 */
export function createCharacterVoice(
  characterId: string,
  payload: Partial<VoiceProfile> & { voice_id: string },
) {
  return post<VoiceProfile>(`/audio/characters/${characterId}/voices`, payload)
}

/** 更新音色配置（修改默认音色不影响已审核通过的历史音频） */
export function updateVoiceProfile(voiceProfileId: string, payload: Partial<VoiceProfile>) {
  return patch<VoiceProfile>(`/audio/voices/${voiceProfileId}`, payload)
}

/** 试听结果：命中缓存时不建任务，直接返回已有音频地址 */
export interface VoicePreviewResult {
  cached: boolean
  voice_profile_id: string
  /** 缓存命中时返回 */
  asset_id?: string
  audio_url?: string
  text?: string
  /** 未命中时返回 */
  job_id?: string
  signature?: string
}

/**
 * 试听音色
 * - 默认走缓存：speaker/语言/情绪指令/文本 均未变时后端直接返回已有音频，不调用 ComfyUI
 * - force=true 强制重新生成并覆盖缓存（供「重新生成」按钮使用）
 */
/**
 * 只读查询试听缓存：不创建任务、不调用 ComfyUI。
 * 用于抽屉打开时判断「是否已有可播放的试听音频」，未命中时前端绝不能自动合成。
 */
export function getVoicePreviewCache(voiceProfileId: string, text?: string) {
  const query = text ? `?text=${encodeURIComponent(text)}` : ''
  return get<VoicePreviewResult>(`/audio/voices/${voiceProfileId}/preview/cache${query}`)
}

export function previewVoice(
  voiceProfileId: string,
  options?: { text?: string; force?: boolean },
) {
  const payload: Record<string, unknown> = {}
  if (options?.text) payload.text = options.text
  if (options?.force) payload.force = true
  return post<VoicePreviewResult>(`/audio/voices/${voiceProfileId}/preview`, payload)
}

/** 列出 TTS 支持的 speaker（来自后端 workflow meta，禁止前端硬编码） */
export function listSpeakers() {
  return get<{ workflow_id: string | null; speakers: SpeakerInfo[] }>('/audio/speakers')
}

/** 获取项目级旁白 speaker 配置 */
export function getNarratorVoice(projectId: string) {
  return get<{ project_id: string; narrator_voice: NarratorVoice | null }>(
    `/audio/projects/${projectId}/narrator-voice`,
  )
}

/** 设置项目级旁白 speaker 配置 */
export function setNarratorVoice(projectId: string, narratorVoice: NarratorVoice) {
  return put<{ project_id: string; narrator_voice: NarratorVoice }>(
    `/audio/projects/${projectId}/narrator-voice`,
    { narrator_voice: narratorVoice },
  )
}

/** 音频资产播放/下载地址（复用资产文件服务端点，mimetypes 自动识别音频类型） */
export function getAudioAssetUrl(assetId: string) {
  // 问题16 修复：不再硬编码 /api 前缀，改由 apiUrl 按部署基路径拼接，避免子路径部署 404
  return apiUrl(`/assets/${assetId}/file`)
}

/** 获取项目级音频策略（native_audio_policy 默认 discard） */
export function getAudioConfig(projectId: string) {
  return get<{ project_id: string; audio_config: AudioConfig }>(
    `/audio/projects/${projectId}/audio-config`,
  )
}

/** 设置项目级音频策略（native_audio_policy ∈ discard/preserve/preserve_ambience_only） */
export function setAudioConfig(projectId: string, audioConfig: Partial<AudioConfig>) {
  return put<{ project_id: string; audio_config: AudioConfig }>(
    `/audio/projects/${projectId}/audio-config`,
    { audio_config: audioConfig },
  )
}
