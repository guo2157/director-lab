import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  autoTitleSession,
  createSession,
  executeApproval,
  listApprovals,
  listMessages,
  listReviews,
  listSessions,
  resolveApproval,
  deleteSession,
  restoreSession,
  updateSession,
} from '@/api/agent'
import type { AgentApproval, AgentMessage, AgentSession, ReviewCard } from '@/api/agent'
import { ApiError, post } from '@/api/client'

/**
 * Agent 会话全局状态（V2 双栏工作台 Phase 2）
 *
 * 职责边界：
 * - 只持有 Agent 侧的会话/消息/审批/审核卡片状态
 * - 不持有任务进度（TaskState 由 useTaskEvents 单例管理）
 * - 不持有布局状态（WorkspaceState 由 stores/workspace.ts 管理）
 * - 不执行路由跳转（统一走 services/workspaceNavigation.ts）
 *
 * 设计原因：右侧助手面板与后续的计划卡片/审批中心都要读写同一份会话状态，
 * 若状态留在组件内，面板一卸载就丢失草稿与消息，且无法跨页面共享。
 */
export const useAgentStore = defineStore('agent', () => {
  // ── 状态 ──
  const sessions = ref<AgentSession[]>([])
  const activeSessionId = ref<string | null>(null)
  const messages = ref<AgentMessage[]>([])
  const approvals = ref<AgentApproval[]>([])
  const reviewCards = ref<ReviewCard[]>([])

  const sessionsLoading = ref(false)
  const messagesLoading = ref(false)
  const sending = ref(false)
  const approvalBusyId = ref<string | null>(null)

  /** 输入框草稿：跨面板收起/展开保留 */
  const draft = ref('')
  /** 本轮导航提示（由导航服务返回，展示在消息流末尾） */
  const navNotice = ref<string | null>(null)

  // ── 面板布局状态 ──
  /**
   * 面板展开态放在 store：跨路由保持，且 AppLayout 需要据此让出右侧空间。
   * 展开时为文档流内的 flex 子项（不再固定覆盖），收起时只保留悬浮按钮。
   */
  const panelOpen = ref(false)
  /** 面板宽度（px），参与 flex 布局，随视口收缩并被夹取 */
  const panelWidth = ref(392)
  const PANEL_MIN_WIDTH = 300
  function panelMaxWidth() {
    if (typeof window === 'undefined') return 760
    return Math.max(PANEL_MIN_WIDTH, Math.min(760, Math.round(window.innerWidth * 0.46)))
  }
  function clampPanelWidth(w: number) {
    return Math.max(PANEL_MIN_WIDTH, Math.min(panelMaxWidth(), Math.round(w)))
  }
  function openPanel() {
    panelWidth.value = clampPanelWidth(panelWidth.value)
    panelOpen.value = true
  }
  function closePanel() {
    panelOpen.value = false
  }
  function togglePanel() {
    if (panelOpen.value) closePanel()
    else openPanel()
  }
  function setPanelWidth(w: number) {
    panelWidth.value = clampPanelWidth(w)
  }

  // ── 派生 ──
  const activeSession = computed(
    () => sessions.value.find((s) => s.id === activeSessionId.value) ?? null,
  )

  const sessionOptions = computed(() =>
    sessions.value.map((s) => ({ label: s.title || '未命名会话', value: s.id })),
  )

  /** 待处理审批（pending 待裁决 / approved 待执行） */
  const pendingApprovals = computed(
    () => approvals.value.filter((a) => a.status === 'pending' || a.status === 'approved'),
  )

  /** 待审核卡片 */
  const pendingReviews = computed(
    () => reviewCards.value.filter((c) => c.status === 'pending'),
  )

  // ── 动作 ──
  async function loadSessions() {
    sessionsLoading.value = true
    try {
      const res = await listSessions()
      sessions.value = res.items ?? []
      if (!activeSessionId.value && sessions.value.length) {
        activeSessionId.value = sessions.value[0].id
      }
    } catch {
      sessions.value = []
    } finally {
      sessionsLoading.value = false
    }
  }

  async function loadMessages() {
    if (!activeSessionId.value) {
      messages.value = []
      return
    }
    messagesLoading.value = true
    try {
      const res = await listMessages(activeSessionId.value)
      messages.value = res.items ?? []
    } catch {
      messages.value = []
    } finally {
      messagesLoading.value = false
    }
  }

  async function loadApprovals() {
    if (!activeSessionId.value) {
      approvals.value = []
      return
    }
    try {
      const res = await listApprovals(activeSessionId.value)
      approvals.value = res.items ?? []
    } catch {
      approvals.value = []
    }
  }

  async function loadReviewCards() {
    if (!activeSessionId.value) {
      reviewCards.value = []
      return
    }
    try {
      const res = await listReviews(activeSessionId.value)
      reviewCards.value = res.items ?? []
    } catch {
      reviewCards.value = []
    }
  }

  /** 切换会话：并行拉取消息、审批、审核卡片 */
  async function switchSession(id: string | null) {
    activeSessionId.value = id
    navNotice.value = null
    await Promise.all([loadMessages(), loadApprovals(), loadReviewCards()])
  }

  async function newSession(projectId?: string | null) {
    const s = await createSession({
      title: '新会话',
      project_id: projectId ?? null,
    })
    await loadSessions()
    await switchSession(s.id)
    return s
  }

  /** 重命名会话（用户手动命名后，自动命名不再覆盖） */
  async function renameSession(id: string, title: string): Promise<void> {
    const next = title.trim()
    if (!next) return
    const updated = await updateSession(id, { title: next })
    const cur = sessions.value.find((s) => s.id === id)
    if (cur) cur.title = updated.title
  }

  /**
   * 删除会话：物理删除会话及其全部对话上下文，不可恢复。
   * 删掉的是当前会话时，自动切到剩余的第一个，没有则清空选中态。
   */
  async function removeSession(id: string): Promise<void> {
    await deleteSession(id)
    const wasActive = activeSessionId.value === id
    await loadSessions()
    if (wasActive) {
      activeSessionId.value = sessions.value[0]?.id ?? null
      if (activeSessionId.value) await switchSession(activeSessionId.value)
      else {
        messages.value = []
        approvals.value = []
        reviewCards.value = []
      }
    }
  }

  /**
   * 首轮对话后按内容自动命名。只在标题仍为默认值时生效（后端判定），
   * 失败静默——命名是锦上添花，不影响聊天。
   */
  async function autoTitle(id: string): Promise<void> {
    try {
      const res = await autoTitleSession(id)
      if (!res.renamed) return
      const cur = sessions.value.find((s) => s.id === id)
      if (cur) cur.title = res.title
    } catch {
      // 命名失败不影响聊天流程
    }
  }

  /** 归档会话列表（「最近删除」，供恢复入口用） */
  const archivedSessions = ref<AgentSession[]>([])

  async function loadArchivedSessions(): Promise<void> {
    try {
      const res = await listSessions({ include_archived: true, page_size: 100 })
      archivedSessions.value = (res.items ?? []).filter((s) => s.status === 'archived')
    } catch {
      archivedSessions.value = []
    }
  }

  /** 恢复归档会话：恢复后切到该会话 */
  async function restoreArchived(id: string): Promise<void> {
    await restoreSession(id)
    await loadSessions()
    await loadArchivedSessions()
    await switchSession(id)
  }

  /**
   * 发送消息。返回 assistant 消息便于调用方处理导航。
   * 不使用乐观消息之外的第二套 API，复用既有 /agent/sessions/{id}/chat。
   */
  async function sendMessage(content: string): Promise<AgentMessage | null> {
    const text = content.trim()
    if (!text || !activeSessionId.value || sending.value) return null

    sending.value = true
    draft.value = ''
    const optimisticId = `local-${Date.now()}`
    messages.value.push({
      id: optimisticId,
      session_id: activeSessionId.value,
      role: 'user',
      content: text,
      payload: null,
      attachment_id: null,
      seq: -1,
      created_at: new Date().toISOString(),
    } as AgentMessage)

    try {
      const res = await post<{ user_message: AgentMessage; assistant_message: AgentMessage }>(
        `/agent/sessions/${activeSessionId.value}/chat`,
        { content: text },
        { timeout: 180000 },
      )
      const idx = messages.value.findIndex((m) => m.id === optimisticId)
      if (idx >= 0) messages.value.splice(idx, 1, res.user_message)
      else messages.value.push(res.user_message)
      messages.value.push(res.assistant_message)
      void loadApprovals()
      // 首轮对话后自动命名（后端判定是否仍为默认标题，已手动命名的不覆盖）
      void autoTitle(activeSessionId.value)
      return res.assistant_message
    } catch (e) {
      // 超时/网络错误时服务端可能已完成（长耗时剧本生成常见），先拉回真实消息再判定
      if (e instanceof ApiError && (e.code === 'timeout' || e.code === 'network_error')) {
        const before = messages.value.length
        await loadMessages()
        if (messages.value.length > before && messages.value[messages.value.length - 1]?.role === 'assistant') {
          return messages.value[messages.value.length - 1] ?? null
        }
      }
      const idx = messages.value.findIndex((m) => m.id === optimisticId)
      if (idx >= 0) messages.value.splice(idx, 1)
      draft.value = text
      throw e
    } finally {
      sending.value = false
    }
  }

  async function decideApproval(a: AgentApproval, approved: boolean) {
    if (!activeSessionId.value) return
    approvalBusyId.value = a.confirmation_id
    try {
      await resolveApproval(activeSessionId.value, a.confirmation_id, { approve: approved })
      await loadApprovals()
    } finally {
      approvalBusyId.value = null
    }
  }

  async function runApproved(a: AgentApproval) {
    if (!activeSessionId.value) return
    approvalBusyId.value = a.confirmation_id
    try {
      await executeApproval(activeSessionId.value, a.confirmation_id)
      await loadApprovals()
    } finally {
      approvalBusyId.value = null
    }
  }

  function setNavNotice(msg: string | null) {
    navNotice.value = msg
  }

  return {
    // 状态
    sessions, activeSessionId, messages, approvals, reviewCards,
    sessionsLoading, messagesLoading, sending, approvalBusyId,
    draft, navNotice, archivedSessions,
    // 面板布局
    panelOpen, panelWidth, openPanel, closePanel, togglePanel, setPanelWidth,
    // 派生
    activeSession, sessionOptions, pendingApprovals, pendingReviews,
    // 动作
    loadSessions, loadMessages, loadApprovals, loadReviewCards,
    switchSession, newSession, sendMessage,
    renameSession, removeSession, autoTitle,
    loadArchivedSessions, restoreArchived,
    decideApproval, runApproved, setNavNotice,
  }
})
