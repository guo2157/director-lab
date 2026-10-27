/** 剧本版本（POST /api/projects/{id}/script-versions：text 1–50000 字符） */
export interface ScriptVersion {
  script_version_id: string
  version: number
  content_hash: string
  created_at: string
}

/** 剧本估算结果（POST /api/projects/{id}/estimate，须与 script_version 哈希一致） */
export interface EpisodeBreakdown {
  episode: number
  estimated_duration: number
  estimated_shots: number
}

export interface ScriptEstimate {
  estimate_id: string
  script_version_id: string
  algorithm_version: string
  char_count: number
  dialogue_char_estimate: number
  estimated_total_duration: number
  estimated_episodes: number
  episode_breakdown: EpisodeBreakdown[]
  exceeds_batch_limit: boolean
  suggestion: string | null
  user_confirmed: boolean
}

/** 解析版本（parse 生成 candidate 候选版本，confirm 乐观锁） */
export interface ParseVersion {
  parse_version_id: string
  script_version_id: string
  status: 'candidate' | 'confirmed' | 'superseded'
  created_at: string
  confirmed_at?: string | null
}

/** 场景（ID 前缀 scn_） */
export interface Scene {
  id: string
  project_id: string
  title: string
  summary?: string
  seq: number
  episodes?: number[]
  shot_count?: number
  baseline_asset_id?: string | null
  created_at: string
  updated_at: string
}

/** 镜头类型枚举（dialogue/narration 时 dialogue 必填） */
export type ShotKind = 'dialogue' | 'narration' | 'visual' | 'transition'

/** 镜头（ID 前缀 sht_，duration 4–15 整数秒） */
export interface Shot {
  id: string
  scene_id: string
  episode: number
  seq: number
  shot_kind: ShotKind
  title?: string
  description?: string
  dialogue?: string
  image_prompt?: string
  video_prompt?: string
  duration: number
  character_id?: string | null
  speaker_id?: string | null
  keyframe_confirmed?: boolean
  current_assets?: ShotCurrentAssets
  consistency?: ShotConsistency
  created_at: string
  updated_at: string
}

/** 一致性（score 0–1，verdict passed/review/failed） */
export interface ShotConsistency {
  score: number
  verdict: 'passed' | 'review' | 'failed'
}

/** 镜头聚合素材（scene/character/props/current_assets 四键，值为 keyframe/clip/audio） */
export interface AssetMaterialRef {
  asset_id: string
  version: number
  file_path?: string
  thumb_path?: string | null
  url?: string
  thumb_url?: string | null
}

/** 镜头聚合素材（与后端返回一致：current_assets.{keyframe, clip, audio} 扁平结构） */
export interface ShotCurrentAssets {
  keyframe?: AssetMaterialRef | null
  clip?: AssetMaterialRef | null
  audio?: AssetMaterialRef | null
}

/** 角色（ID 前缀 chr_，name 项目内唯一） */
export interface Character {
  id: string
  project_id: string
  name: string
  description?: string
  portrait_asset_id?: string | null
  created_at: string
  updated_at: string
}

/** 道具（ID 前缀 prp_，name 项目内唯一） */
export interface Prop {
  id: string
  project_id: string
  name: string
  description?: string
  anchor_asset_id?: string | null
  created_at: string
  updated_at: string
}

/** 项目镜头列表项（GET /api/projects/{id}/shots，附 scene_title/scene_seq） */
export interface ProjectShotItem extends Shot {
  scene_title: string
  scene_seq: number
}

/** 剧本解析相关负载 */
export interface CreateScriptVersionPayload {
  text: string
}

export interface ParsePayload {
  script_version_id: string
  text: string
  episode_start: number
  /** 两种输入模式二选一：episode_count（起始+数量）或 episode_end（起止集数） */
  episode_count?: number
  episode_end?: number
  replace_existing?: boolean
}

/** 集级解析记录（集数网格视图） */
export interface EpisodeParseStatusItem {
  episode_no: number
  asset_type: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  attempt_count: number
  error_msg: string | null
  asset_count: number | null
  parse_version_id: string | null
  updated_at: string | null
}

export interface ParseStatus {
  total_episodes: number | null
  records: EpisodeParseStatusItem[]
}
