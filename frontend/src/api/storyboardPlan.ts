/** 分镜规划导入（Phase 2）：契约校验预览与安全写入。
 *
 * 校验为只读；写入为显式 upsert：锁定镜头关键字段不覆盖、规划外旧镜头不删除、
 * 不重建资产计划、不触发生成。参考槽位与 Prompt 编译仍由生产层负责。
 */
import { post } from './client'

export interface StoryboardPlanIssue {
  level: 'error' | 'warning' | 'info'
  code: string
  path: string
  message: string
  related_ids?: string[]
  source_block_ids?: string[]
}

export interface StoryboardPlanCoverage {
  scenes: {
    total: number
    covered: number
    uncovered_scene_ids: string[]
  }
  beats: {
    total: number
    covered: number
    uncovered_block_ids: string[]
    by_kind: Record<string, {
      total: number
      covered: number
      uncovered_block_ids: string[]
    }>
  }
}

export interface StoryboardPlanFieldChange {
  /** Shot 列名，如 seq / duration / dialogue */
  field: string
  from: unknown
  to: unknown
}

export interface StoryboardPlanFieldChangeGroup {
  shot_id: string
  /** 已锁定且关键字段变化：写入时会整体跳过该镜头 */
  will_skip: boolean
  fields: StoryboardPlanFieldChange[]
}

export interface StoryboardPlanComparison {
  existing_shot_count: number
  existing_locked_shot_ids: string[]
  reused_shot_ids: string[]
  removed_shot_ids: string[]
  sequence_conflicts: Array<{ scene_id: string; seq: number; shot_id: string }>
  locked_conflicts: Array<{ shot_id: string; fields: string[] }>
  /** 写入前逐镜头字段差异；只含规划显式提供且值有变化的字段 */
  field_changes: StoryboardPlanFieldChangeGroup[]
  duration_total_seconds: number
}

export interface StoryboardPlanValidateResult {
  algorithm: string
  script_version_id: string
  script_version: number
  plan_version: number | null
  valid: boolean
  issues: StoryboardPlanIssue[]
  coverage: StoryboardPlanCoverage
  adapted_shots: Array<Record<string, unknown>>
  comparison: StoryboardPlanComparison
}

export interface StoryboardPlanApplyResult extends StoryboardPlanValidateResult {
  algorithm: string
  mode: string
  applied: boolean
  created_shot_ids: string[]
  updated_shot_ids: string[]
  unchanged_shot_ids: string[]
  skipped_locked: Array<{ shot_id: string; fields: string[] }>
  orphan_shot_ids: string[]
  conflicts: Array<{
    scene_id: string
    episode: number
    seq: number
    shot_id: string
    conflicts_with: string
  }>
  notes?: string[]
}

export function validateStoryboardPlan(
  projectId: string,
  scriptVersionId: string,
  plan: unknown,
): Promise<StoryboardPlanValidateResult> {
  return post<StoryboardPlanValidateResult>(
    `/projects/${projectId}/script-versions/${scriptVersionId}/storyboard-plan/validate`,
    { plan },
  )
}

export function applyStoryboardPlan(
  projectId: string,
  scriptVersionId: string,
  plan: unknown,
): Promise<StoryboardPlanApplyResult> {
  return post<StoryboardPlanApplyResult>(
    `/projects/${projectId}/script-versions/${scriptVersionId}/storyboard-plan/apply`,
    { plan },
  )
}
