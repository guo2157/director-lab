/**
 * 导演台展示层辅助函数：从 assistant 消息 payload 中提取可展示的真实执行数据。
 *
 * 只做纯读取与格式化，不含请求逻辑；主视图与子组件共用，避免同一份 payload
 * 解析规则在两处漂移。
 */
import type { AgentMessage } from '@/api/agent'
import type { TaskCardData } from '@/api/tasks'

/** 时间戳展示：月-日 时:分（不显示秒，避免元信息行抖动） */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 气泡内可展示的资产引用（由后端 assistant payload.asset_refs 提供） */
export interface AssetRef {
  asset_id: string
  asset_type: string
  name: string
  has_thumb: boolean
  validity: string
}

export function assetRefsOf(m: AgentMessage): AssetRef[] {
  const p = m.payload as { asset_refs?: AssetRef[] } | null
  return Array.isArray(p?.asset_refs) ? (p.asset_refs as AssetRef[]) : []
}

/** 视频类资产无法用 img 直出，降级为链接卡片 */
export function isImageAsset(t: string): boolean {
  return !['clip', 'video'].includes(t)
}

/** 消息 payload 中的任务卡片（覆盖 parse/generate/compose/export） */
export function taskCardOf(m: AgentMessage): TaskCardData | null {
  if (m.role !== 'assistant') return null
  const p = m.payload as { task_card?: TaskCardData } | null
  return p?.task_card ?? null
}

/** 联网搜索执行记录（由后端 assistant payload.search 提供，真实执行数据非 LLM 生成） */
export interface SearchRecord {
  search_run_id: string
  status: string
  query: string
  provider: string | null
  result_count: number
  valid_result_count: number
  attempt_count: number
  duration_ms: number | null
  error: string | null
}

export function searchRecordOf(m: AgentMessage): SearchRecord | null {
  const p = m.payload as { search?: SearchRecord } | null
  return p?.search ?? null
}

export const SEARCH_STATUS_TEXT: Record<string, string> = {
  success: '搜索完成',
  partial: '部分成功（部分来源读取失败）',
  empty: '无有效结果',
  failed: '搜索失败',
}

export const SEARCH_PHASE_TEXT: Record<string, string> = {
  searching: '搜索中',
  validating: '验证结果',
  fetching: '读取正文',
  summarizing: '汇总结果',
}
