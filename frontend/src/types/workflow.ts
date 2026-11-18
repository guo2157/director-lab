/** 动态工作流前端类型（P5）——与后端 /api/workflows/{id}/schema、/capabilities 对齐。 */

export type WorkflowType = 'image' | 'video' | 'tts' | 'bgm' | 'sfx'

/** 参数类型（后端 schema_builder 产出） */
export type WorkflowParamType =
  | 'string' | 'textarea' | 'integer' | 'number' | 'boolean'
  | 'select' | 'enum' | 'image' | 'audio' | 'video'

export interface WorkflowParamSchema {
  key: string
  label: string
  type: WorkflowParamType
  semantic?: string
  required?: boolean
  default?: string | number | boolean | null
  min?: number
  max?: number
  step?: number
  options?: Array<string | number>
  placeholder?: string
  description?: string
  group?: 'basic' | 'generation' | 'reference' | 'advanced' | string
  order?: number
  advanced?: boolean
  visible_if?: Record<string, unknown>
}

export interface WorkflowOutputSchema {
  key: string
  label: string
  media_type: 'image' | 'video' | 'audio'
  multiple: boolean
  required: boolean
}

/** GET /api/workflows/{id}/schema */
export interface WorkflowSchema {
  workflow_id?: string
  workflow_name?: string
  workflow_type?: WorkflowType
  version?: number
  schema_version: string
  parser_version?: string
  /** 标量参数：动态表单主体 */
  parameters: WorkflowParamSchema[]
  /** 图片参考槽位（前端只做展示/绑定，node 绑定在后端） */
  reference_inputs: WorkflowParamSchema[]
  audio_inputs: WorkflowParamSchema[]
  video_inputs: WorkflowParamSchema[]
  /** 全量平铺（兼容旧消费方） */
  params: WorkflowParamSchema[]
  outputs: WorkflowOutputSchema[]
  errors: string[]
  warnings: string[]
}

export interface WorkflowAudioCapability {
  accepts_tts_reference_audio: boolean
  max_audio_inputs: number
  audio_output_mode: string | null
  supports_native_audio: boolean
  voice_mode?: string | null
}

export interface WorkflowVideoCapability {
  supports_first_frame: boolean
  supports_last_frame: boolean
  supports_previous_tail_frame: boolean
  duration_control: boolean
  frame_count_range: number[] | null
  fps: number | null
}

export interface WorkflowSlotCapability {
  param: string
  input_key: string | null
  optional: boolean
  semantic: string | null
  media_type: 'image' | 'audio' | 'video'
}

/** GET /api/workflows/{id}/capabilities */
export interface WorkflowCapability {
  workflow_id?: string
  workflow_name?: string
  version?: number
  workflow_type: WorkflowType
  generation_modes: string[]
  modalities: Record<string, boolean>
  audio: WorkflowAudioCapability
  video: WorkflowVideoCapability
  reference: {
    supports_multi_reference: boolean
    max_image_references: number
    slots: WorkflowSlotCapability[]
  }
  constraints: Record<string, unknown>
  source: Record<string, unknown>
}

export interface WorkflowVersionItem {
  id: string
  workflow_id: string
  version: number
  file_hash: string | null
  schema_version: string | null
  validation_status: string
  runtime_status: string
  changelog: string | null
  is_current: boolean
  created_at: string | null
  schema_summary: { params: number; outputs: string[] }
}

export interface RuntimeCheckResult {
  workflow_id: string
  version: number
  runtime_status: 'ready' | 'missing_deps' | 'unreachable' | 'unchecked'
  instance: string | null
  checked_nodes?: number
  required_nodes?: number
  required_models?: number
  missing_node_types: string[]
  missing_models: string[]
  error?: string
}

/** 能力标签的中文文案（选择器与能力卡片共用） */
export const MODE_LABELS: Record<string, string> = {
  text_to_image: '文生图',
  image_to_image: '图生图',
  multi_reference: '多参考图',
  text_to_video: '文生视频',
  image_to_video: '图生视频',
  reference_to_video: '参考生视频',
  first_last_frame_to_video: '首尾帧',
  audio_conditioned_video: '音频条件',
  text_to_speech: '语音合成',
  text_to_music: '音乐生成',
  text_to_sound: '音效生成',
}
