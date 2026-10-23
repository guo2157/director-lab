/** 任务类型（job_type 枚举） */
export type JobType =
  | 'generate_keyframe'
  | 'generate_clip'
  | 'generate_audio'
  | 'generate_bgm'
  | 'generate_sfx'
  | 'parse'
  | 'compose'
  | 'export'

/** 任务状态 */
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'lost' | 'cancelled'

/** 任务（ID 前缀 job_，retry 仅 failed/lost 且 retryable=true） */
export interface Job {
  id: string
  project_id?: string | null
  job_type: JobType
  status: JobStatus
  target_id?: string | null
  workflow_id?: string | null
  input_snapshot?: Record<string, unknown>
  result?: Record<string, unknown> | null
  error?: {
    code: string
    message: string
    detail?: Record<string, unknown> | string | null
  } | null
  retryable?: boolean
  attempts?: number
  seq?: number
  created_at: string
  updated_at: string
  finished_at?: string | null
}

/** 任务统计（GET /api/jobs/stats：avg_duration_seconds 近 7 日，无数据 null） */
export interface JobStats {
  today_succeeded: number
  today_failed: number
  in_progress: number
  avg_duration_seconds: number | null
}

/** 生成目标枚举（POST /api/shots/{id}/generate） */
export type GenerateTarget = 'keyframe' | 'clip' | 'audio' | 'bgm' | 'sfx'

/** 生成模式（缺省按 target 与项目设置定，响应返回最终模式） */
export type GenerationMode = 'text_only' | 'image_to_video' | 'reference_guided'

/** 镜头生成参数 */
export interface GenerateShotPayload {
  target: GenerateTarget
  workflow_id?: string
  generation_mode?: GenerationMode
  /** 兼容字段 */
  use_reference?: boolean
}

/** 生成响应（202：job + effective_inputs） */
export interface GenerateResponse {
  job: Job
  effective_inputs: Record<string, unknown>
}

/** 批量生成响应（202） */
export interface BatchGenerateResponse {
  job_ids: string[]
  effective_inputs?: Record<string, unknown>
}
