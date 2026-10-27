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
