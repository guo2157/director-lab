/** 多图参考重新生成工作台 V1 接口封装。
 * 约定：
 * - 配置读取从指定基础版本加载原始生成配置，不从最新版本拼装
 * - 重新生成结果一律先落候选版本，采用后才切换为当前生效版本
 */
import { get, post } from './client'

export type RegenerateMode = 'inherit_prompt_only' | 'modify_refs' | 'full_reconfigure'

export interface RefSlotInfo {
  param: string
  input_key?: string | null
  required: boolean
  semantic: 'reference' | 'frame_constraint'
  role?: string | null
  system?: boolean
}

export interface RegenerateRef {
  slot_param: string
  input_key?: string | null
  asset_id?: string | null
  asset_version?: number | null
  asset_type?: string | null
  asset_name?: string | null
  role?: string | null
  required?: boolean
  file_path?: string | null
  semantic?: 'reference' | 'frame_constraint'
  enabled?: boolean
}
/** 一致性校验单项问题 */
export interface ConsistencyIssue {
  code: string
  image_no?: number
  slot?: string
  message: string
}
/** 一致性校验通过项 */
export interface ConsistencyCheck {
  image_no: number
  slot: string
  status: string
  message: string
}
/** 提交前预检结果：errors 非空阻止提交，warnings 需用户确认 */
export interface ValidateResult {
  valid: boolean
  errors: ConsistencyIssue[]
  warnings: ConsistencyIssue[]
  checks: ConsistencyCheck[]
}

export interface RegenerateConfig {
  base: {
    asset_id: string
    asset_type: string
    name: string
    version: number
    validity: string
    file_path?: string | null
    source_job_id?: string | null
  } | null
  snapshot_complete: boolean
  incomplete_reasons: string[]
  mode: RegenerateMode
  prompt: { rendered: string; editable: string }
  negative_prompt: { rendered: string; supported: boolean }
  workflow: { id: string | null; name: string | null; type: string | null; version: number | null }
  available_slots: RefSlotInfo[]
  refs: RegenerateRef[]
  model_params: Record<string, unknown>
  locked: { refs: boolean; params: boolean; workflow: boolean }
  editable: { prompt: boolean; negative_prompt: boolean; refs: boolean; params: boolean; workflow: boolean }
  modes: RegenerateMode[]
}

export interface RegenerateJob {
  id: string
  project_id: string
  shot_id: string | null
  job_type: string
  status: string
  workflow_id: string | null
  workflow_version: number | null
  created_at: string
  effective_inputs?: Record<string, unknown> | null
  snapshot?: Record<string, unknown> | null
}

export interface CandidateAsset {
  asset_id: string
  version: number
  file_path?: string | null
  thumb_path?: string | null
  source_job_id?: string | null
  created_at?: string | null
}

/** 读取指定基础版本的原始生成配置 */
export function fetchRegenerateConfig(
  shotId: string,
  target: 'keyframe' | 'clip',
  baseAssetId?: string,
): Promise<RegenerateConfig> {
  const params: Record<string, unknown> = { target }
  if (baseAssetId) params.base_asset_id = baseAssetId
  return get<RegenerateConfig>(`/shots/${shotId}/regenerate/config`, params)
}

/** 提交重新生成任务（结果落候选版本） */
export function submitRegenerate(shotId: string, payload: Record<string, unknown>): Promise<RegenerateJob> {
  return post<RegenerateJob>(`/shots/${shotId}/regenerate/workbench`, payload)
}

/** 查看任务冻结的输入快照 */
export function fetchJobSnapshot(jobId: string): Promise<Record<string, unknown>> {
  return get<Record<string, unknown>>(`/jobs/${jobId}/snapshot`)
}

/** 列出候选版本 */
export function fetchCandidates(assetId: string): Promise<{ asset_id: string; candidates: CandidateAsset[] }> {
  return get<{ asset_id: string; candidates: CandidateAsset[] }>(`/assets/${assetId}/candidates`)
}

/** 采用候选版本 */
export function adoptCandidate(assetId: string): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`/assets/${assetId}/adopt`)
}

/** 查询更换尾帧或参考图后受影响的后续镜头 */
export function fetchImpact(shotId: string): Promise<{ shot_id: string; affected: Array<Record<string, unknown>> }> {
  return get<{ shot_id: string; affected: Array<Record<string, unknown>> }>(`/shots/${shotId}/impact`)
}
/** 提交前预检：校验提示词图片引用与参考图槽位一致性（不创建任务） */
export function validateRegenerate(
  shotId: string,
  payload: Record<string, unknown>,
): Promise<ValidateResult> {
  return post<ValidateResult>(`/shots/${shotId}/regenerate/validate`, payload)
}
