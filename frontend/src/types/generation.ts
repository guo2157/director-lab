/** 生成进度：后端 /api/projects/{id}/asset-generation-progress 与
 *  /api/projects/{id}/keyframe-generation-progress 的共同口径。
 *
 * 前端用它回答三个问题：
 * 1. 正在生成什么（inflight 明细，带 kind / entity_name / progress）
 * 2. 批量生成到哪一步了（done / total / inflight_count / failed_recent_count）
 * 3. 什么时候全部生成完（has_inflight 变 false 且 all_done 为 true）
 */

/** 资产图分组（公共资产：角色定妆图 / 场景基准图 / 道具锚定图） */
export interface GenerationGroupStat {
  label: string
  total: number
  done: number
}

/** 资产图在途明细：kind 区分角色/场景/道具，entity_name 供直接展示「正在生成什么」 */
export interface AssetGenerationJob {
  job_id: string
  kind: 'character' | 'scene' | 'prop' | string
  kind_label: string
  entity_id: string
  entity_name: string
  status: string
  /** running 任务的进度百分比（0-100），其余为空 */
  progress: number | null
  error_msg?: string | null
  created_at?: string | null
  started_at?: string | null
}

export interface AssetGenerationFailure {
  job_id: string
  kind: string
  entity_id: string
  entity_name: string
  error_msg?: string | null
  finished_at?: string | null
}

export interface AssetGenerationProgress {
  project_id: string
  total: number
  done: number
  percent: number
  all_done: boolean
  started: boolean
  groups: Partial<Record<'character' | 'scene' | 'prop', GenerationGroupStat>>
  inflight_count: number
  inflight_queued: number
  inflight_running: number
  inflight: AssetGenerationJob[]
  failed_recent_count: number
  failed_recent: AssetGenerationFailure[]
  has_inflight: boolean
  last_completed_at?: string | null
  assets_review_status?: string
}

/** 分镜图在途明细（镜头级） */
export interface KeyframeGenerationJob {
  job_id: string
  shot_id: string | null
  shot_seq: number | null
  episode: number | null
  scene_id: string | null
  scene_title: string | null
  status: string
  progress: number | null
  error_msg?: string | null
  created_at?: string | null
  started_at?: string | null
}

export interface KeyframeGenerationFailure {
  job_id: string
  shot_id: string | null
  shot_seq: number | null
  scene_title: string | null
  error_msg?: string | null
  finished_at?: string | null
}

/** 分镜视频在途明细（镜头级）：queue_rank 为排队名次，running 为 0 */
export interface ClipGenerationJob {
  job_id: string
  shot_id: string | null
  shot_seq: number | null
  episode: number | null
  scene_id: string | null
  scene_title: string | null
  status: string
  progress: number | null
  queue_rank: number
  error_msg?: string | null
  created_at?: string | null
  started_at?: string | null
}

export interface ClipGenerationFailure {
  job_id: string
  shot_id: string | null
  shot_seq: number | null
  scene_title: string | null
  error_msg?: string | null
  finished_at?: string | null
}

export interface ClipGenerationProgress {
  project_id: string
  total: number
  with_clip: number
  done: number
  percent: number
  all_done: boolean
  started: boolean
  inflight_count: number
  inflight_queued: number
  inflight_running: number
  inflight_chained_wait: number
  inflight: ClipGenerationJob[]
  /** 近 7 日单镜平均耗时（秒），无数据为空 */
  avg_seconds?: number | null
  /** 剩余队列预计耗时（秒） */
  estimated_wait_seconds?: number | null
  failed_recent_count: number
  failed_recent: ClipGenerationFailure[]
  has_inflight: boolean
  last_completed_at?: string | null
}

export interface KeyframeGenerationProgress {
  project_id: string
  total: number
  with_keyframe: number
  done: number
  confirmed: number
  percent: number
  all_done: boolean
  all_confirmed: boolean
  started: boolean
  inflight_count: number
  inflight_queued: number
  inflight_running: number
  inflight: KeyframeGenerationJob[]
  failed_recent_count: number
  failed_recent: KeyframeGenerationFailure[]
  has_inflight: boolean
  last_completed_at?: string | null
}
