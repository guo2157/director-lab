import type { WorkflowType } from './asset'

/** 字幕模式（burn=烧录 / sidecar=外挂，默认 burn） */
export type SubtitleMode = 'burn' | 'sidecar'

/** 导出清晰度档位（默认 standard） */
export type DeliveryProfile = 'standard' | 'high_quality'

/** 时间轴轨道条目（compose timeline_tracks） */
export interface TimelineTrack {
  role: string
  asset_id: string
  asset_version?: number
  shot_id?: string
  start?: number
  end?: number
  volume?: number
  fade?: number
  loop_policy?: string
}

/** 合成参数（POST /api/projects/{id}/compose，返回 202） */
export interface ComposePayload {
  episode?: number
  include_subtitle?: boolean
  subtitle_mode?: SubtitleMode
  bgm?: {
    asset_id?: string
    volume?: number
  }
  timeline_tracks?: TimelineTrack[]
  compose_tolerance?: number
}

/** 合成结果（result 含 composition_id/file_path/duration/srt_path/warnings） */
export interface Composition {
  composition_id: string
  episode: number
  status: 'composing' | 'done' | 'failed'
  stale: boolean
  file_path?: string
  duration?: number
  srt_path?: string
  warnings?: string[]
  created_at: string
}

/** 导出参数（composition_id 必填，须有效未过期） */
export interface ExportPayload {
  composition_id: string
  format?: string
  delivery_profile?: DeliveryProfile
}

/** 导出记录（download_url 为 /api/exports/{id}/download 完整路径） */
export interface ExportRecord {
  id: string
  composition_id: string
  file_name?: string
  size?: number
  status: 'exporting' | 'done' | 'failed'
  download_url: string
  created_at: string
}

/** 语言模型提供方（默认 DeepSeek） */
export type LlmProvider = 'deepseek' | 'openai' | 'qwen' | 'ollama' | 'custom'

/** 全局设置（密钥掩码 + api_key_set；密钥传空字符串=清除） */
export interface AppSettings {
  comfyui_base_url?: string
  llm_base_url?: string
  llm_provider?: LlmProvider
  llm_model?: string
  llm_api_key?: string
  llm_api_key_set?: boolean
  job_max_attempts?: number
  default_concurrency?: number
  [key: string]: unknown
}

/** ComfyUI 实例（多实例加权负载均衡） */
export interface ComfyUIInstance {
  base_url: string
  weight: number
  enabled: boolean
}
/** 实例连通性测试结果 */
export interface ComfyUIInstanceTestResult {
  base_url: string
  enabled: boolean
  weight: number
  ok: boolean
  latency_ms?: number
  reason?: string
  message?: string
}

/** 连接测试失败原因枚举 */
export type TestFailureReason =
  | 'connection_refused'
  | 'timeout'
  | 'unauthorized'
  | 'invalid_response'
  | 'dns_error'

/** 健康检查（GET /api/health：status/database/comfyui/worker/disk） */
export interface HealthStatus {
  status: string
  database: string
  comfyui: string
  comfyui_instances?: ComfyUIInstanceHealth[]
  worker: string
  disk: string
}

/** 单个 ComfyUI 实例健康状态 */
export interface ComfyUIInstanceHealth {
  base_url: string
  enabled: boolean
  weight: number
  status: string
}

/** 清理上传缓存结果（60 秒频控，重复调用返回上次结果 + cached:true） */
export interface ClearCacheResult {
  freed_bytes: number
  removed_files: number
  skipped_in_use: number
  cached?: boolean
}

/** 工作流类型重导出（compose 模块引用） */
export type { WorkflowType }
