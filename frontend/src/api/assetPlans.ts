import { get, post } from './client'

/** 资产定义类型：公共资产为 character/scene/prop，集级资产为 keyframe/clip */
export type PlanType = 'character' | 'scene' | 'prop' | 'keyframe' | 'clip'
export type PlanStatus = 'pending' | 'generated' | 'rejected'

export interface PlanDependency {
  slot: string
  ref_key: string
  ref_path?: string | null
  ref_plan_id?: string | null
}

/** 资产定义：名称与占位路径一经生成不再变更，后续替换只改内容 */
export interface AssetPlan {
  id: string
  plan_type: PlanType
  asset_key: string
  media_path: string
  thumb_path?: string | null
  prompt?: string | null
  status: PlanStatus
  owner_type?: string | null
  owner_id?: string | null
  episode?: number | null
  seq?: number | null
  asset_id?: string | null
  version: number
  confirmed: boolean
  dependencies?: PlanDependency[]
}

export interface PlanCount {
  total: number
  pending: number
  generated: number
  rejected: number
}

export interface AssetPlanListResult {
  items: AssetPlan[]
  counts: Record<string, PlanCount>
  total: number
}

export interface GenerateFromPlansResult {
  jobs: Array<{ job_id: string; status: string; job_type: string }>
  count: number
  skipped: Array<{ plan_id: string; reason: string }>
}

/** 查询项目内资产定义（可按类型、状态、集数过滤，可选带依赖） */
export function listAssetPlans(
  projectId: string,
  params?: {
    plan_type?: PlanType
    status?: PlanStatus
    episode?: number
    with_dependencies?: boolean
  },
) {
  return get<AssetPlanListResult>(`/projects/${projectId}/asset-plans`, params)
}

/** 按定义批量触发生成；plan_ids 缺省取该类型全部待生成定义 */
export function generateFromPlans(
  projectId: string,
  payload: { plan_type: PlanType; plan_ids?: string[]; workflow_id?: string },
) {
  return post<GenerateFromPlansResult>(`/projects/${projectId}/asset-plans/generate`, payload)
}

/** 人工确认某条定义（分镜图确认后放行对应分镜视频） */
export function confirmPlan(projectId: string, planId: string) {
  return post<{ plan_id: string; confirmed: boolean }>(
    `/projects/${projectId}/asset-plans/${planId}/confirm`,
  )
}

/** 取消人工确认 */
export function unconfirmPlan(projectId: string, planId: string) {
  return post<{ plan_id: string; confirmed: boolean }>(
    `/projects/${projectId}/asset-plans/${planId}/unconfirm`,
  )
}
