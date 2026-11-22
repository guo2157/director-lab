/** 依赖图只读查询（Phase 3）：影响预演接口封装。
 * 后端服务为纯只读：不标记失效、不删除产物、不自动重生成。
 */
import { get } from './client'

export type DependencyChangeStatus =
  | 'redo_required'
  | 'rebind_required'
  | 'not_generated'
  | 'stale_reference'
  | 'unknown_version'
  | 'up_to_date'

export interface DependencyStaleInput {
  edge_id: string
  source: string
  source_version?: number | null
  current_version?: number | null
  reason: 'dependency_invalid' | 'source_version_changed' | 'upstream_needs_redo'
}

export interface DependencyAffectedNode {
  key: string
  type: string
  id: string
  version?: number | null
  status?: string | null
  depth: number
  path: string[]
  via?: { edge_id: string; record_type: string; role?: string | null } | null
  change_status: DependencyChangeStatus
  has_output: boolean
  requires_regeneration: boolean
  stale_inputs: DependencyStaleInput[]
}

export interface DependencyImpactPreview {
  algorithm: string
  root: {
    key: string
    type: string
    id: string
    version?: number | null
    lineage_versions: number[]
  }
  policy: {
    auto_regenerate: boolean
    mark_stale: boolean
    delete_existing: boolean
    writes: boolean
    note: string
  }
  affected: DependencyAffectedNode[]
  diagnostics: Array<Record<string, unknown>>
  summary: {
    affected_count: number
    redo_required: number
    rebind_required: number
    not_generated: number
    up_to_date: number
    unknown_version: number
  }
}

export interface DependencyProjectImpactPreview {
  algorithm: string
  policy: DependencyImpactPreview['policy']
  changed_roots: Array<{
    key: string
    type: string
    id: string
    version?: number | null
  }>
  affected: DependencyAffectedNode[]
  diagnostics: Array<Record<string, unknown>>
  summary: DependencyImpactPreview['summary']
}

/** 依赖影响状态文案（影响面板、看板卡片、资产变更影响页共用） */
export const IMPACT_STATUS_LABEL: Record<DependencyChangeStatus, string> = {
  redo_required: '需重生成',
  rebind_required: '需重绑定',
  not_generated: '未生成',
  stale_reference: '引用过期',
  unknown_version: '版本未知',
  up_to_date: '已最新',
}

/** 依赖节点类型文案 */
export const IMPACT_NODE_TYPE_LABEL: Record<string, string> = {
  asset: '资产',
  asset_plan: '资产定义',
  shot: '镜头',
  job: '任务',
  composition: '合成',
  export: '导出',
}

/** 镜头卡片上可提示的依赖状态（未生成/已最新不在镜头卡片上告警） */
export const IMPACT_SHOT_LABEL: Partial<Record<DependencyChangeStatus, string>> = {
  redo_required: '依赖已过期，需重做',
  rebind_required: '资产版本更新，需重绑',
  stale_reference: '依赖引用失效',
}

/** 把项目级预演结果映射为 shotId → 状态。
 * 镜头节点直接映射；任务等产物节点沿 path 中的 `shot:` 键回溯到镜头，
 * 需重做优先于其他状态。仅前端派生，不推断后端未返回的依赖。 */
export function buildShotImpactMap(
  data: Pick<DependencyProjectImpactPreview, 'affected'>,
): Map<string, DependencyChangeStatus> {
  const map = new Map<string, DependencyChangeStatus>()
  for (const node of data.affected) {
    if (!IMPACT_SHOT_LABEL[node.change_status]) continue
    if (node.type === 'shot') {
      if (!map.has(node.id)) map.set(node.id, node.change_status)
      continue
    }
    for (const key of node.path) {
      if (!key.startsWith('shot:')) continue
      const shotId = key.slice('shot:'.length)
      const prev = map.get(shotId)
      if (node.change_status === 'redo_required' || prev === undefined) {
        map.set(shotId, node.change_status)
      }
    }
  }
  return map
}

export function getDependencyImpactPreview(
  projectId: string,
  nodeType: string,
  nodeId: string,
): Promise<DependencyImpactPreview> {
  return get<DependencyImpactPreview>(
    `/projects/${projectId}/dependency-graph/${nodeType}/${nodeId}/impact-preview`,
  )
}

/** 项目级只读总览：所有因上游版本/失效变化而待重做、待重绑的对象 */
export function getProjectImpactPreview(projectId: string): Promise<DependencyProjectImpactPreview> {
  return get<DependencyProjectImpactPreview>(
    `/projects/${projectId}/dependency-graph/impact-preview`,
  )
}
