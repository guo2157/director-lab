import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  createSession,
  executeApproval,
  listApprovals,
  listMessages,
  listReviews,
  listSessions,
  resolveApproval,
} from '@/api/agent'
import type { AgentApproval, AgentMessage, AgentSession, ReviewCard } from '@/api/agent'
import { post } from '@/api/client'

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
      )
      const idx = messages.value.findIndex((m) => m.id === optimisticId)
      if (idx >= 0) messages.value.splice(idx, 1, res.user_message)
      else messages.value.push(res.user_message)
      messages.value.push(res.assistant_message)
      void loadApprovals()
      return res.assistant_message
    } catch (e) {
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
    draft, navNotice,
    // 面板布局
    panelOpen, panelWidth, openPanel, closePanel, togglePanel, setPanelWidth,
    // 派生
    activeSession, sessionOptions, pendingApprovals, pendingReviews,
    // 动作
    loadSessions, loadMessages, loadApprovals, loadReviewCards,
    switchSession, newSession, sendMessage,
    decideApproval, runApproved, setNavNotice,
  }
})
