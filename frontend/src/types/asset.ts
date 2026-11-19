/** 资产类型（event_type 与 asset_type 枚举互不通用） */
export type AssetType = 'character' | 'scene' | 'prop' | 'keyframe' | 'clip' | 'audio' | 'last_frame' | 'reference' | 'first_frame' | 'end_frame'

/** 资产状态 */
export type AssetStatus = 'pending' | 'current' | 'stale' | 'archived'

/** 手动绑定 role（仅 4 种，其余由 generate 内部自动注入） */
export type ManualAssetRole = 'reference' | 'first_frame' | 'end_frame' | 'audio'

/** 资产（ID 前缀 ast_，上传单文件 ≤500MB） */
export interface Asset {
  id: string
  project_id: string
  asset_type: AssetType
  /** 资产名称（后端 _to_out 返回，角色/场景/道具为实体名称） */
  name?: string
  status: AssetStatus
  version: number
  file_path?: string
  thumb_path?: string | null
  url?: string
  thumb_url?: string | null
  mime_type?: string
  size_bytes?: number
  locked: boolean
  reference_count?: number
  source_job_id?: string | null
  created_at: string
  updated_at: string
}

/** 资产版本（版本时间线可回滚） */
export interface AssetVersion {
  version: number
  asset_id: string
  file_path?: string
  thumb_path?: string | null
  url?: string
  thumb_url?: string | null
  created_at: string
  source_job_id?: string | null
}

/** 工作流类型（workflow_type 枚举 image/video/tts/bgm/sfx） */
export type WorkflowType = 'image' | 'video' | 'tts' | 'bgm' | 'sfx'

/** 工作流（ID 前缀 wf_，multipart 上传 ≤10MB，PATCH 生成不可变新版本） */
export interface Workflow {
  id: string
  name: string
  alias?: string | null
  workflow_type: WorkflowType
  description?: string
  /** 描述来源：seed（内置播种）/ user（用户修改）；用于 Seed 不覆盖用户描述 */
  description_source?: 'seed' | 'user' | null
  version: number
  is_default: boolean
  is_builtin?: boolean
  definition?: Record<string, unknown>
  /** 工作流能力声明（reference_slots / negative 相关槽位等），由后端返回 */
  capabilities?: Record<string, unknown> | null
  parsed_schema?: WorkflowParsedSchema
  used_by_jobs?: number
  created_at: string
  updated_at: string
}

/** 工作流解析结果（POST /api/workflows 返回 parsed_schema.params/outputs） */
export interface WorkflowParsedSchema {
  params: Array<{
    key: string
    label?: string
    type?: string
    default?: unknown
    required?: boolean
  }>
  outputs: Array<{
    key: string
    label?: string
    type?: string
  }>
}
