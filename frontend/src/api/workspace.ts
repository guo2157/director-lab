import { get } from './client'

/**
 * 工作台事件（/api/workspace/events SSE 与 backfill，seq 严格递增）
 *
 * 与 TaskEvent 的区别：
 * - TaskEvent 描述单个 Task 的执行阶段（task_id 维度）
 * - WorkspaceEvent 描述工作台层跨对象事实（导航、审批、页面上下文）
 * 两者并存，互不替代。
 */
export interface WorkspaceEvent {
  seq: number
  event_type: string
  session_id: string | null
  project_id: string | null
  route_name: string | null
  created_at: string | null
  [key: string]: unknown
}

/** 事件类型白名单（与后端 services/workspace_event.py 的 EVENT_TYPES 对齐） */
export const WORKSPACE_EVENT_TYPES = {
  navigate: 'workspace.navigate',
  pageContext: 'workspace.page_context',
  approvalPending: 'workspace.approval_pending',
  approvalResolved: 'workspace.approval_resolved',
  approvalExecuted: 'workspace.approval_executed',
  taskUpdated: 'workspace.task_updated',
} as const

/** 工作台事件断线补齐：seq 严格大于给定值，按 seq 升序 */
export function backfillWorkspaceEvents(updatedSinceSeq: number, sessionId?: string | null) {
  const params: Record<string, string | number> = { updated_since_seq: updatedSinceSeq }
  if (sessionId) params.session_id = sessionId
  return get<{ total: number; items: WorkspaceEvent[] }>('/workspace/events/backfill', params)
}
