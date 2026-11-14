/** 多图参考重新生成工作台 V1 接口封装。
 * 约定：
 * - 配置读取从指定基础版本加载原始生成配置，不从最新版本拼装
 * - 分镜图/视频重新生成结果直接替换当前生效版本，没有候选版本环节
 */
import { get, post, put } from './client'

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

/** 提交重新生成任务（结果直接替换当前版本） */
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

/* ============================================================
 * 重新生成工作台：多参考图 / 槽位绑定 / 身份绑定 / 空间布局 /
 * 动作时间轴 / 分层提示词 / 连续性校验 / 参数快照
 * ============================================================ */

/** 工作台可用槽位（来自工作流 definition 的真实注入标记，不在前端补造） */
export interface WorkspaceSlot {
  param: string
  node_id: string | null
  input_key: string | null
  image_no: number | null
  required: boolean | null
}

/** 工作台结构化参考图绑定 */
export interface WorkspaceReference {
  reference_id: string
  slot_id: string
  node_id?: string | null
  input_key?: string | null
  image_no?: number | null
  reference_type?: string | null
  asset_type?: string | null
  asset_id?: string | null
  asset_name?: string | null
  character_id?: string | null
  image_url?: string | null
  binding_description?: string | null
  identity_constraints?: string[]
  required?: boolean
  enabled?: boolean
  sort_order?: number
  semantic?: 'reference' | 'frame_constraint'
  status?: string
}

/** 角色身份绑定：解决『谁是谁』 */
export interface WorkspaceCharacterBinding {
  character_id: string | null
  character_name: string | null
  identity_reference_id?: string | null
  identity_slot_id?: string | null
  identity_image_no?: number | null
  identity_constraints?: string[]
  status?: string
}

/** 空间位置（屏幕归一化坐标） */
export interface WorkspacePosition {
  horizontal?: 'left' | 'center' | 'right' | null
  vertical?: 'top' | 'center' | 'bottom' | null
  depth?: 'foreground' | 'middle_ground' | 'background' | null
  x?: number | null
  y?: number | null
  facing_direction?: string | null
  status: 'confirmed' | 'pending_confirmation'
}

/** 角色空间布局：解决『谁在哪里』 */
export interface WorkspaceSpatialCharacter {
  character_id: string
  character_name: string
  initial_position: WorkspacePosition
  final_position: WorkspacePosition
  facing_direction?: string | null
  movement_enabled?: boolean
  movement_direction?: string | null
  movement_path?: Array<Record<string, unknown>>
  relative_relations?: Array<Record<string, unknown>>
}

export interface WorkspaceSpatialLayout {
  coordinate_system?: string
  aspect_ratio?: string
  camera_view?: string
  characters: WorkspaceSpatialCharacter[]
  prop_positions?: Array<Record<string, unknown>>
  status?: string
  source?: string
  has_last_frame?: boolean
}

/** 动作时间轴：解决『谁做什么』 */
export interface WorkspaceAction {
  action_id: string
  character_id?: string | null
  character_name?: string | null
  start_progress: number
  end_progress: number
  action_description: string
  status: string
}

export interface WorkspaceActionTimeline {
  duration_seconds?: number | null
  actions: WorkspaceAction[]
  status?: string
  source?: string
}

/** 道具绑定 */
export interface WorkspacePropBinding {
  prop_id?: string | null
  prop_name?: string | null
  reference_id?: string | null
  reference_slot_id?: string | null
  holder_character_id?: string | null
  holder_character_name?: string | null
  visibility?: string
  attachment_position?: string | null
  follows_character?: boolean
  handover?: string | null
  status?: string
}

/** 连续性校验结论（只提示，不自动改写） */
export interface WorkspaceContinuityIssue {
  code: string
  level: 'error' | 'warning' | 'info'
  message: string
  character_id?: string
  character_name?: string
  prop_id?: string
  prop_name?: string
}

export interface WorkspaceContinuity {
  status: 'conflict' | 'ok' | 'first_shot'
  previous_shot_id: string | null
  previous_episode: number | null
  previous_seq: number | null
  previous_config_found: boolean
  issues: WorkspaceContinuityIssue[]
  summary: string
  auto_applied: boolean
}

/** 工作流能力声明：H3 ref2va 仅支持提示词级空间/时间约束 */
export interface WorkspaceCapabilities {
  spatial_control: string
  timeline_control: string
  reason?: string
}

export interface WorkspaceHistoryCompat {
  has_saved_config: boolean
  saved_version: number | null
  base_snapshot_complete: boolean
  incomplete_reasons: string[]
  base_asset_id: string | null
}

/** 工作台完整数据 */
export interface RegenerateWorkspace {
  shot_id: string
  target: string
  source: 'saved' | 'derived'
  workflow: { id?: string | null; name?: string | null; type?: string | null; version?: number | null }
  base: Record<string, unknown> | null
  snapshot_complete: boolean
  incomplete_reasons: string[]
  available_slots: WorkspaceSlot[]
  references: WorkspaceReference[]
  character_bindings: WorkspaceCharacterBinding[]
  spatial_layout: WorkspaceSpatialLayout
  action_timeline: WorkspaceActionTimeline
  prop_bindings: WorkspacePropBinding[]
  prompt_sections: Record<string, string>
  final_prompt: string
  parameters: Record<string, unknown>
  validation: ValidateResult & { summary?: Record<string, number> }
  continuity: WorkspaceContinuity
  capabilities: WorkspaceCapabilities
  shot: Record<string, unknown>
  history_compat: WorkspaceHistoryCompat
  /** 串联生成首帧衔接：ready=上一镜尾帧可作本镜首帧；missing=上一镜尚无尾帧；not_applicable=集内首镜 */
  chain_frame?: { prev_shot_id: string | null; status: 'ready' | 'missing' | 'not_applicable' }
}

/** 工作台校验结果 */
export interface WorkspaceValidateResult {
  valid: boolean
  errors: ConsistencyIssue[]
  warnings: ConsistencyIssue[]
  checks: ConsistencyCheck[]
  workspace: {
    summary: Record<string, number>
    continuity: WorkspaceContinuity
    capabilities: WorkspaceCapabilities
  }
}

/** 读取工作台完整数据（有保存配置优先用保存内容） */
export function fetchRegenerateWorkspace(
  shotId: string,
  target: 'keyframe' | 'clip' = 'clip',
): Promise<RegenerateWorkspace> {
  return get<RegenerateWorkspace>(`/shots/${shotId}/regenerate/workspace`, { target })
}

/** 保存工作台草稿（只存可编辑副本，不创建任务、不改写历史视频） */
export function saveRegenerateWorkspace(
  shotId: string,
  payload: Record<string, unknown>,
): Promise<RegenerateWorkspace> {
  return put<RegenerateWorkspace>(`/shots/${shotId}/regenerate/workspace`, payload)
}

/** 工作台提交前预检：结构化校验 + 槽位校验 + 提示词一致性 */
export function validateRegenerateWorkspace(
  shotId: string,
  payload: Record<string, unknown>,
): Promise<WorkspaceValidateResult> {
  return post<WorkspaceValidateResult>(`/shots/${shotId}/regenerate/workspace/validate`, payload)
}

/** 按工作台配置提交重新生成任务（结果直接替换当前生效视频，无候选版本） */
export function submitRegenerateWorkspace(
  shotId: string,
  payload: Record<string, unknown>,
): Promise<RegenerateJob & { workspace_config_id?: string; continuity?: WorkspaceContinuity }> {
  return post<RegenerateJob & { workspace_config_id?: string; continuity?: WorkspaceContinuity }>(
    `/shots/${shotId}/regenerate/workspace/regenerate`,
    payload,
  )
}
