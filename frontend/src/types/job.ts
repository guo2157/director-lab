/** 任务类型（job_type 枚举，与后端实际产出的全量取值对齐）
 *  script_agent / series_agent 是 Agent 链路落库的真实类型：
 *  「Agent 智能分析」走 script_agent，其 result 与 parse 同构（scenes/characters/props）。 */
export type JobType =
  | 'generate_keyframe'
  | 'generate_clip'
  | 'generate_audio'
  | 'generate_bgm'
  | 'generate_sfx'
  | 'parse'
  | 'script_agent'
  | 'series_agent'
  | 'compose'
  | 'export'

/** 任务状态 */
export type JobStatus = 'queued' | 'running' | 'chained_wait' | 'submission_unknown' | 'succeeded' | 'failed' | 'lost' | 'cancelled'

/** 任务（ID 前缀 job_，retry 仅 failed/lost 且 retryable=true） */
export interface Job {
  id: string
  project_id?: string | null
  /** 关联镜头 ID（生成任务必填，供按镜头关联任务状态） */
  shot_id?: string | null
  /** 展示用关联信息（后端列表/详情接口补充） */
  project_name?: string | null
  episode?: number | null
  scene_title?: string | null
  shot_seq?: number | null
  job_type: JobType
  status: JobStatus
  target_id?: string | null
  workflow_id?: string | null
  input_snapshot?: Record<string, unknown>
  result?: Record<string, unknown> | null
  /** 错误码（后端平铺字段，如 llm_error/comfy_timeout） */
  error_code?: string | null
  /** 错误信息（后端平铺字段，展示给用户的具体原因） */
  error_msg?: string | null
  retryable?: boolean
  attempts?: number
  seq?: number
  /** 生成进度百分比（0-100）：running 任务从最近 job.progress 事件计算，其余状态为空 */
  progress?: number | null
  /** 绝对推进量（已接收字符数）：总量未知时用它证明任务在推进 */
  progress_chars?: number | null
  created_at: string
  updated_at: string
  /** 任务真正开始执行的时间（后端返回）；耗时计算以此为准，缺失时回退 created_at */
  started_at?: string | null
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
