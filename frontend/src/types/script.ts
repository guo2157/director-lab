/** 剧本版本（POST /api/projects/{id}/script-versions：text 1–50000 字符） */
export interface ScriptVersion {
  script_version_id: string
  version: number
  content_hash: string
  created_at: string
}

/** 剧本估算结果（POST /api/projects/{id}/estimate，须与 script_version 哈希一致） */
export interface ScriptEstimate {
  estimate_id: string
  script_version_id: string
  episode_count: number
  scene_count: number
  shot_count: number
  estimated_duration_seconds: number
  created_at: string
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
  url?: string
}

export interface ShotCurrentAssets {
  scene?: {
    keyframe?: AssetMaterialRef | null
    clip?: AssetMaterialRef | null
    audio?: AssetMaterialRef | null
  }
  character?: {
    keyframe?: AssetMaterialRef | null
    clip?: AssetMaterialRef | null
    audio?: AssetMaterialRef | null
  }
  props?: Array<{
    prop_id: string
    keyframe?: AssetMaterialRef | null
    clip?: AssetMaterialRef | null
    audio?: AssetMaterialRef | null
  }>
  current_assets?: Array<{
    asset_id: string
    role: string
    version: number
    url?: string
  }>
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
  episode_from?: number
  episode_to?: number
  replace_existing?: boolean
}
