import { del, get, patch, post } from './client'

/** 导演台会话 */
export interface AgentSession {
  id: string
  title: string
  project_id: string | null
  status: string
  model: string | null
  created_at: string
  updated_at: string
  message_count: number
  last_message_at: string | null
}

/** 导演台消息（role: user/assistant/system/tool_event/approval） */
export interface AgentMessage {
  id: string
  session_id: string
  role: string
  content: string
  payload: Record<string, unknown> | null
  attachment_id: string | null
  seq: number
  created_at: string
}

export interface SessionListResult {
  items: AgentSession[]
  total: number
  page: number
  page_size: number
}

export interface MessageListResult {
  items: AgentMessage[]
  total: number
  page: number
  page_size: number
}

/** 创建会话 */
export function createSession(payload: { title?: string; project_id?: string | null; model?: string | null }) {
  return post<AgentSession>('/agent/sessions', payload)
}

/** 会话列表（默认不含归档） */
export function listSessions(params?: {
  project_id?: string
  include_archived?: boolean
  page?: number
  page_size?: number
}) {
  return get<SessionListResult>('/agent/sessions', params)
}

/** 会话详情 */
export function getSession(id: string) {
  return get<AgentSession>(`/agent/sessions/${id}`)
}

/** 重命名 / 关联项目 / 切换模型 */
export function updateSession(
  id: string,
  payload: { title?: string; project_id?: string | null; model?: string | null },
) {
  return patch<AgentSession>(`/agent/sessions/${id}`, payload)
}

/** 归档（软删除） */
export function archiveSession(id: string) {
  return del<{ id: string; status: string }>(`/agent/sessions/${id}`)
}

/** 恢复归档会话 */
export function restoreSession(id: string) {
  return post<AgentSession>(`/agent/sessions/${id}/restore`)
}

/** 追加消息 */
export function appendMessage(
  sessionId: string,
  payload: {
    role: string
    content?: string
    payload?: Record<string, unknown> | null
    attachment_id?: string | null
  },
) {
  return post<AgentMessage>(`/agent/sessions/${sessionId}/messages`, payload)
}

/** 消息列表（after_seq 增量加载） */
export function listMessages(
  sessionId: string,
  params?: { after_seq?: number; page?: number; page_size?: number },
) {
  return get<MessageListResult>(`/agent/sessions/${sessionId}/messages`, params)
}

/** 待审批的生成申请 */
export interface AgentApproval {
  confirmation_id: string
  task_id: string
  tool_name: string
  args: Record<string, unknown> | null
  status: string
  comment: string | null
  created_at: string | null
}

/** 会话内待审批列表 */
export function listApprovals(sessionId: string) {
  return get<{ items: AgentApproval[]; count: number }>(
    `/agent/sessions/${sessionId}/approvals`,
  )
}

/** 裁决审批：approve=true 批准，false 拒绝（批准不触发生成） */
export function resolveApproval(
  sessionId: string,
  confirmationId: string,
  payload: { approve: boolean; comment?: string | null },
) {
  return post<AgentApproval>(
    `/agent/sessions/${sessionId}/approvals/${confirmationId}/resolve`,
    payload,
  )
}

/** 执行已批准的生成申请（幂等，重复调用复用同一任务） */
export function executeApproval(sessionId: string, confirmationId: string) {
  return post<{ confirmation_id: string; job_id: string; status: string; reused: boolean }>(
    `/agent/sessions/${sessionId}/approvals/${confirmationId}/execute`,
  )
}

// ── 剧本/资产生成审核卡片（统一确认事项） ──

/** 审核卡片数据结构（对应后端 review_confirmation.to_card 返回值） */
export interface ReviewCard {
  confirmation_id: string
  kind: string
  confirmation_type: string
  target_type: string
  target_id: string | null
  target_version: number | null
  round: number | null
  status: string
  question: string | null
  allowed_responses: string[] | null
  user_response: string | null
  comment: string | null
  resolution: string | null
  created_at: string | null
  resolved_at: string | null
}

/** 审核卡片列表结果 */
export interface ReviewListResult {
  items: ReviewCard[]
  count: number
}

/** 列出会话内所有审核卡片（含待审核、已通过、已驳回、修改中、已取消、已过期） */
export function listReviews(sessionId: string) {
  return get<ReviewListResult>(`/agent/sessions/${sessionId}/reviews`)
}

/** 裁决审核（通过 / 修改 / 驳回） */
export function resolveReview(
  sessionId: string,
  confirmationId: string,
  payload: { resolution: string; comment?: string | null },
) {
  return post<ReviewCard>(`/agent/sessions/${sessionId}/reviews/${confirmationId}/resolve`, payload)
}
