<script setup lang="ts">
/**
 * 导演台（会话工作台）。
 *
 * 布局：PageHeader（标题 + 当前会话模型 + 主操作「新建会话」）
 *      + 三栏：会话导航 / 消息流（含审核时间线 + 输入区）/ 任务上下文。
 *
 * 本文件只保留数据获取、SSE 订阅与状态机；呈现层拆到 components/director/。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { NButton, NSelect } from 'naive-ui'
import { ApiError, get, post } from '@/api/client'
import {
  archiveSession,
  createSession,
  executeApproval,
  listApprovals,
  listMessages,
  listReviews,
  listSessions,
  resolveApproval,
  restoreSession,
  updateSession,
} from '@/api/agent'
import type { AgentApproval, AgentMessage, AgentSession, ReviewCard } from '@/api/agent'
import { getProjectStyleConfig, getStyleOptions } from '@/api/projects'
import type { StyleConfig, StyleOptions } from '@/types/project'
import { useTaskEvents } from '@/composables/useTaskEvents'
import PageHeader from '@/components/ui/PageHeader.vue'
import StatusTag from '@/components/ui/StatusTag.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import SessionRail from '@/components/director/SessionRail.vue'
import MessageStream from '@/components/director/MessageStream.vue'
import ChatComposer from '@/components/director/ChatComposer.vue'
import ReviewTimeline from '@/components/director/ReviewTimeline.vue'
import ContextSidebar from '@/components/director/ContextSidebar.vue'
import { taskCardOf } from '@/components/director/helpers'
import type { ProjectBrief } from '@/components/director/types'

const message = useMessage()
const route = useRoute()
const router = useRouter()

// --- 面板折叠 ---
const sessionsCollapsed = ref(typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches)
const contextCollapsed = ref(false)

// --- 左栏：历史会话导航（唯一职责：找到/切换/管理会话） ---
const sessions = ref<AgentSession[]>([])
const sessionsLoading = ref(false)
const sessionsError = ref('')
const activeSessionId = ref<string | null>(null)
const activeSession = computed(
  () => sessions.value.find((s) => s.id === activeSessionId.value) ?? null,
)

const headerSubtitle = computed(() =>
  activeSession.value
    ? `当前会话：${activeSession.value.title}`
    : '选择或新建会话，开始与导演台助手协作',
)

async function loadSessions() {
  sessionsLoading.value = true
  sessionsError.value = ''
  try {
    const res = await listSessions({ page_size: 50 })
    sessions.value = res.items
    // 未选中、或选中的会话已失效（归档/删除）时，自动选中第一个可用会话；
    // 否则输入框会因 activeSessionId 为空而永久禁用，用户会误以为页面坏了
    const stillExists = sessions.value.some((s) => s.id === activeSessionId.value)
    if (!stillExists) {
      activeSessionId.value = sessions.value[0]?.id ?? null
    }
  } catch (e) {
    sessionsError.value = e instanceof Error ? e.message : String(e)
    message.error(`加载会话列表失败: ${sessionsError.value}`)
  } finally {
    sessionsLoading.value = false
  }
}

/*
 * 已归档会话（「最近删除」）。
 * 为什么需要：删除会话实际是置 archived，接口默认只返回 active，
 * 没有这个入口时用户会以为会话被彻底删除、无法找回。
 */
const archivedSessions = ref<AgentSession[]>([])
const archivedLoading = ref(false)

async function loadArchivedSessions() {
  if (archivedLoading.value) return
  archivedLoading.value = true
  try {
    const res = await listSessions({ include_archived: true, page_size: 100 })
    archivedSessions.value = (res.items ?? []).filter((s) => s.status === 'archived')
  } catch {
    // 归档列表是辅助入口，失败不打断主流程
    archivedSessions.value = []
  } finally {
    archivedLoading.value = false
  }
}

async function restoreArchivedSession(s: AgentSession) {
  try {
    await restoreSession(s.id)
    await Promise.all([loadSessions(), loadArchivedSessions()])
    activeSessionId.value = s.id
    message.success(`已恢复会话「${s.title}」`)
  } catch (e) {
    message.error(`恢复会话失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function newSession() {
  try {
    const s = await createSession({ title: '新会话' })
    sessions.value.unshift(s)
    activeSessionId.value = s.id
    // 手机端新建会话后自动收起，回到聊天布局
    if (window.matchMedia('(max-width: 768px)').matches) sessionsCollapsed.value = true
  } catch (e) {
    message.error(`创建会话失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

/** 选择会话：手机端选择后自动收起会话浮层，回到聊天布局 */
function selectSession(s: AgentSession) {
  activeSessionId.value = s.id
  if (window.matchMedia('(max-width: 768px)').matches) sessionsCollapsed.value = true
}

async function renameSession(s: AgentSession, title: string) {
  try {
    const updated = await updateSession(s.id, { title })
    const idx = sessions.value.findIndex((item) => item.id === updated.id)
    if (idx >= 0) sessions.value[idx] = updated
  } catch (e) {
    message.error(`重命名失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function removeSession(s: AgentSession) {
  try {
    await archiveSession(s.id)
    // 删除即归档：同步刷新已归档列表，用户才能在同屏看到并可恢复
    await Promise.all([loadSessions(), loadArchivedSessions()])
    if (activeSessionId.value === s.id) {
      activeSessionId.value = sessions.value[0]?.id ?? null
      messages.value = []
      await loadContext()
    }
    message.success('会话已删除')
  } catch (e) {
    message.error(`删除失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

// --- 中栏：对话与执行主区 ---
const messages = ref<AgentMessage[]>([])
const messagesLoading = ref(false)
const messagesError = ref('')

// --- 统一任务卡片：消费 assistant payload.task_card + /api/tasks/events SSE（单卡片动态更新，不刷屏） ---
const { taskSnapshots, registerCard, connect: connectTaskEvents } = useTaskEvents()

/** 消息加载/新消息到达时注册卡片初始快照（SSE 事件在其上动态更新） */
watch(
  messages,
  () => {
    for (const m of messages.value) {
      const card = taskCardOf(m)
      if (card) registerCard(card)
    }
  },
  { deep: true },
)
onMounted(() => {
  connectTaskEvents()
  void loadArchivedSessions()
})

const inputText = ref('')
const sending = ref(false)
const scrollEl = ref<HTMLElement | null>(null)
/** 用户是否在底部附近：仅在底部时才自动跟随新消息，尊重浏览历史位置 */
const nearBottom = ref(true)

function onScroll() {
  const el = scrollEl.value
  if (!el) return
  nearBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function scrollToBottom(force = false) {
  void nextTick(() => {
    if (!force && !nearBottom.value) return
    const el = scrollEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function loadMessages(sessionId: string) {
  messagesLoading.value = true
  messagesError.value = ''
  try {
    const res = await listMessages(sessionId, { page_size: 200 })
    messages.value = res.items
    scrollToBottom(true)
  } catch (e) {
    messagesError.value = e instanceof Error ? e.message : String(e)
    message.error(`加载消息失败: ${messagesError.value}`)
  } finally {
    messagesLoading.value = false
  }
}

function retryMessages() {
  if (activeSessionId.value) void loadMessages(activeSessionId.value)
}

watch(activeSessionId, (id) => {
  messages.value = []
  messagesError.value = ''
  if (id) void loadMessages(id)
})

/** 思考过程展示：等待响应期间分阶段推进状态，响应到达后由最终消息体替换 */
const thinking = ref(false)
const thinkingStage = ref('')
const THINKING_STAGES = ['正在理解你的意图…', '正在查询项目状态…', '正在组织回复…']
let thinkingTimer: ReturnType<typeof setInterval> | null = null
function startThinking() {
  thinking.value = true
  let stageIdx = 0
  thinkingStage.value = THINKING_STAGES[0]
  thinkingTimer = setInterval(() => {
    stageIdx = Math.min(stageIdx + 1, THINKING_STAGES.length - 1)
    thinkingStage.value = THINKING_STAGES[stageIdx]
  }, 2500)
}
function stopThinking() {
  if (thinkingTimer) {
    clearInterval(thinkingTimer)
    thinkingTimer = null
  }
  thinking.value = false
}

async function send() {
  const content = inputText.value.trim()
  if (!content || !activeSessionId.value || sending.value) return
  sending.value = true
  inputText.value = ''
  // 乐观更新：用户消息立即上屏，等待期间展示思考过程，消除阻塞感
  const optimisticId = `local-${Date.now()}`
  messages.value.push({
    id: optimisticId,
    session_id: activeSessionId.value,
    role: 'user',
    content,
    payload: null,
    attachment_id: null,
    seq: -1,
    created_at: new Date().toISOString(),
  })
  scrollToBottom(true)
  startThinking()
  try {
    const chatRes = await post<{
      user_message: AgentMessage
      assistant_message: AgentMessage
    }>(`/agent/sessions/${activeSessionId.value}/chat`, { content })
    stopThinking()
    // 用服务端消息替换乐观消息；思考气泡由最终回复无缝接管
    const optimisticIdx = messages.value.findIndex((m) => m.id === optimisticId)
    if (optimisticIdx >= 0) messages.value.splice(optimisticIdx, 1, chatRes.user_message)
    else messages.value.push(chatRes.user_message)
    messages.value.push(chatRes.assistant_message)

    // Phase 2: Agent 驱动的双栏导航——当 assistant payload 包含 navigate_workspace 时触发路由跳转
    const navPayload = chatRes.assistant_message.payload as Record<string, unknown> | null
    if (navPayload?.route_action === 'navigate_workspace' && typeof navPayload.route_name === 'string') {
      const projectId = route.params.id as string | undefined
      if (projectId && navPayload.route_name !== 'director' && navPayload.route_name !== 'projects') {
        void router.push({ name: navPayload.route_name, params: { id: projectId } })
      } else {
        void router.push({ name: navPayload.route_name })
      }
    }

    scrollToBottom(true)
    // 首条消息后自动命名：标题仍为默认『新会话』时，用用户消息摘要命名
    const cur = sessions.value.find((s) => s.id === activeSessionId.value)
    if (cur && (cur.title === '新会话' || !cur.title.trim())) {
      const firstLine = content.split('\n').map((l) => l.trim()).find((l) => l) ?? '新会话'
      const autoTitle = firstLine.length > 20 ? `${firstLine.slice(0, 20)}…` : firstLine
      try {
        const updated = await updateSession(activeSessionId.value, { title: autoTitle })
        cur.title = updated.title
      } catch {
        // 命名失败不影响聊天流程
      }
    }
    await loadSessions()
    await loadContext()
    void loadReviewCards()
  } catch (e) {
    stopThinking()
    // 失败回滚：移除乐观消息并恢复输入
    const optimisticIdx = messages.value.findIndex((m) => m.id === optimisticId)
    if (optimisticIdx >= 0) messages.value.splice(optimisticIdx, 1)
    if (e instanceof ApiError) {
      message.error(`发送失败: ${e.message}`)
    } else {
      message.error(`发送失败: ${e instanceof Error ? e.message : String(e)}`)
    }
    inputText.value = content
  } finally {
    sending.value = false
  }
}

// --- 模型选择器（单一控件，写入会话 model 字段，后端按会话覆盖默认模型） ---
const modelOptions = ref<Array<{ label: string; value: string }>>([])
const modelsLoading = ref(false)
// 后端配置的默认模型：会话未单独指定时，实际生效的就是它
const configModel = ref<string | null>(null)

async function loadModels() {
  modelsLoading.value = true
  try {
    // 并行获取：可用模型列表 + 当前配置的默认模型（后者失败不阻断对话）
    const [modelsRes, settingsRes] = await Promise.all([
      post<{ ok: boolean; models: string[] }>('/settings/llm-models', {}),
      get<{ llm_model?: string | null }>('/settings').catch(() => null),
    ])
    const available = modelsRes.models ?? []
    const fallback = settingsRes?.llm_model || null
    configModel.value = fallback
    // 配置默认模型置顶并标注，其余可用模型随后列出
    const opts: Array<{ label: string; value: string }> = []
    if (fallback) opts.push({ label: `${fallback}（默认）`, value: fallback })
    for (const m of available) {
      if (m === fallback) continue
      opts.push({ label: m, value: m })
    }
    modelOptions.value = opts
  } catch {
    modelOptions.value = []
    message.warning('模型列表加载失败，将使用后端默认模型')
  } finally {
    modelsLoading.value = false
  }
}

// 下拉框展示值：会话级指定 > 后端配置默认模型；两者皆无才显示占位符
const modelSelectValue = computed(() => activeSession.value?.model ?? configModel.value ?? null)

async function applyModel(model: string | null) {
  if (!activeSession.value) return
  try {
    const updated = await updateSession(activeSession.value.id, { model })
    const idx = sessions.value.findIndex((s) => s.id === updated.id)
    if (idx >= 0) sessions.value[idx] = updated
    message.success(
      model
        ? `已切换模型：${model}`
        : `已恢复默认模型${configModel.value ? `：${configModel.value}` : ''}`,
    )
  } catch (e) {
    message.error(`切换模型失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

// --- 右栏：当前会话关联的项目/任务上下文（随会话切换，无关联显示空状态） ---
const projects = ref<ProjectBrief[]>([])
// 生成类操作的待审批申请（批准与执行分离：批准只放行，执行才建任务）
const approvals = ref<AgentApproval[]>([])
const approvalLoading = ref(false)
const approvingId = ref<string | null>(null)

// ── 审核记录（时间线呈现，折叠逻辑在 ReviewTimeline 内） ──
const reviewCards = ref<ReviewCard[]>([])
const reviewCardsLoading = ref(false)

async function loadReviewCards() {
  const sid = activeSessionId.value
  reviewCards.value = []
  if (!sid) return
  reviewCardsLoading.value = true
  try {
    const res = await listReviews(sid)
    reviewCards.value = res.items ?? []
  } catch {
    reviewCards.value = []
  } finally {
    reviewCardsLoading.value = false
  }
}

const linkedProject = computed(
  () => projects.value.find((p) => p.id === activeSession.value?.project_id) ?? null,
)

async function loadProjects() {
  try {
    const res = await get<{ items: ProjectBrief[] }>('/projects', { page_size: 50 })
    projects.value = res.items
  } catch (e) {
    message.error(`加载项目失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

/* ---------- 风格基线（只读摘要，完整编辑在剧本页） ---------- */
const styleConfig = ref<StyleConfig | null>(null)
const styleLabel = ref('')
const styleLoading = ref(false)
const styleOptions = ref<StyleOptions | null>(null)

async function loadStyleConfig(pid: string) {
  styleLoading.value = true
  try {
    const [cfg, opts] = await Promise.all([
      getProjectStyleConfig(pid),
      styleOptions.value ? Promise.resolve(null) : getStyleOptions().catch(() => null),
    ])
    styleConfig.value = cfg.config
    styleLabel.value = cfg.visual_style_label
    if (opts) styleOptions.value = opts
  } catch {
    styleConfig.value = null
  } finally {
    styleLoading.value = false
  }
}

async function loadContext() {
  const pid = activeSession.value?.project_id
  styleConfig.value = null
  if (!pid) return
  void loadStyleConfig(pid)
}

async function loadApprovals() {
  const sid = activeSessionId.value
  approvals.value = []
  if (!sid) return
  approvalLoading.value = true
  try {
    const res = await listApprovals(sid)
    approvals.value = res.items ?? []
  } catch {
    approvals.value = []
  } finally {
    approvalLoading.value = false
  }
}

async function decideApproval(a: AgentApproval, approve: boolean) {
  const sid = activeSessionId.value
  if (!sid) return
  approvingId.value = a.confirmation_id
  try {
    await resolveApproval(sid, a.confirmation_id, { approve })
    message.success(approve ? '已批准，可点击执行' : '已拒绝')
    await loadApprovals()
  } catch (e) {
    message.error(`审批失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    approvingId.value = null
  }
}

async function runApproved(a: AgentApproval) {
  const sid = activeSessionId.value
  if (!sid) return
  approvingId.value = a.confirmation_id
  try {
    const res = await executeApproval(sid, a.confirmation_id)
    message.success(res.reused ? `任务已存在：${res.job_id}` : `已创建任务：${res.job_id}`)
    await Promise.all([loadApprovals(), loadContext()])
  } catch (e) {
    message.error(`执行失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    approvingId.value = null
  }
}

watch(
  () => activeSession.value?.project_id,
  () => void loadContext(),
)
watch(activeSessionId, () => {
  void loadApprovals()
  void loadReviewCards()
})

async function linkProject(pid: string | null) {
  // 未选中会话时无法关联：给出明确提示，避免点击后毫无反馈被误认为报错
  if (!activeSession.value) {
    message.warning('请先选择或新建一个会话，再关联项目')
    return
  }
  try {
    const updated = await updateSession(activeSession.value.id, { project_id: pid })
    const idx = sessions.value.findIndex((s) => s.id === updated.id)
    if (idx >= 0) sessions.value[idx] = updated
    message.success(pid ? '已关联项目' : '已取消关联')
    await loadContext()
  } catch (e) {
    message.error(`关联失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

function goProject(page: string) {
  const pid = activeSession.value?.project_id
  if (pid) void router.push(`/project/${pid}/${page}`)
}

onMounted(() => {
  void loadSessions()
  void loadProjects()
  void loadModels()
})
</script>

<template>
  <div class="console-page">
    <PageHeader title="导演台" :subtitle="headerSubtitle">
      <template #status>
        <StatusTag v-if="!activeSession" status="locked" label="未选择会话" />
        <template v-else>
          <span class="model-caption">模型</span>
          <NSelect
            :value="modelSelectValue"
            :options="modelOptions"
            :loading="modelsLoading"
            :disabled="!activeSessionId"
            size="small"
            :placeholder="configModel ? `默认：${configModel}` : '默认模型'"
            clearable
            class="model-select"
            @update:value="(v: string | null) => applyModel(v)"
          />
        </template>
      </template>

      <template #actions>
        <NButton
          size="small"
          quaternary
          class="session-toggle"
          aria-label="打开会话列表"
          @click="sessionsCollapsed = false"
        >
          会话
        </NButton>
        <NButton size="small" type="primary" @click="newSession">新建会话</NButton>
      </template>
    </PageHeader>

    <div class="console-body">
      <!-- 左栏：会话导航 -->
      <SessionRail
        v-if="!sessionsCollapsed"
        :sessions="sessions"
        :loading="sessionsLoading"
        :active-id="activeSessionId"
        :error="sessionsError"
        :archived="archivedSessions"
        @select="selectSession"
        @create="newSession"
        @rename="renameSession"
        @remove="removeSession"
        @restore="restoreArchivedSession"
        @collapse="sessionsCollapsed = true"
        @retry="loadSessions"
      />
      <button v-else class="expand-btn left" type="button" @click="sessionsCollapsed = false">
        会话 ›
      </button>

      <!-- 中栏：对话与执行主区 -->
      <section class="panel chat-panel">
        <div ref="scrollEl" class="chat-scroll" @scroll="onScroll">
          <div v-if="messagesLoading" class="chat-state">
            <SkeletonList variant="row" :rows="6" />
          </div>
          <div v-else-if="messagesError" class="chat-state">
            <ErrorRetry
              title="消息加载失败"
              :message="messagesError"
              @retry="retryMessages"
            />
          </div>
          <div v-else-if="!activeSessionId" class="chat-state">
            <EmptyState
              title="未选择会话"
              description="选择左侧会话，或新建一个会话开始与导演台助手协作"
              action-text="新建会话"
              @action="newSession"
            />
          </div>
          <div v-else-if="!messages.length && !thinking" class="chat-state">
            <EmptyState
              title="还没有消息"
              description="发送第一条消息，描述你要推进的创作任务"
            />
          </div>
          <MessageStream
            v-else
            :messages="messages"
            :thinking="thinking"
            :thinking-stage="thinkingStage"
            :snapshots="taskSnapshots"
          />
        </div>

        <!-- 审核记录：同类连续记录合并为时间线，默认只展开最新一组 -->
        <ReviewTimeline
          v-if="reviewCards.length || (reviewCardsLoading && !!activeSessionId)"
          :cards="reviewCards"
          :loading="reviewCardsLoading"
        />

        <ChatComposer
          v-model="inputText"
          :disabled="!activeSessionId || sending"
          :loading="sending"
          @send="send"
        />
      </section>

      <!-- 右栏：当前会话的项目/任务上下文（三个折叠面板） -->
      <ContextSidebar
        v-if="!contextCollapsed"
        :project="linkedProject"
        :has-session="!!activeSession"
        :projects="projects"
        :style-config="styleConfig"
        :style-label="styleLabel"
        :style-options="styleOptions"
        :style-loading="styleLoading"
        :approvals="approvals"
        :approval-loading="approvalLoading"
        :approving-id="approvingId"
        @link="linkProject"
        @go="goProject"
        @decide="decideApproval"
        @run="runApproved"
        @collapse="contextCollapsed = true"
      />
      <button v-else class="expand-btn right" type="button" @click="contextCollapsed = false">
        ‹ 上下文
      </button>
    </div>
  </div>
</template>

<style scoped>
.console-page {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  height: 100%;
  min-height: 0;
  padding: var(--sp-4) var(--sp-5) var(--sp-5);
  max-width: 1600px;
  margin: 0 auto;
  box-sizing: border-box;
}

.console-body {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: var(--sp-3);
  align-items: stretch;
}

/* 头部间距交给 .console-page 的 gap，避免与 PageHeader 自身 margin 叠加 */
.console-page :deep(.page-header) {
  margin-bottom: 0;
}

.session-toggle {
  display: none;
}

.model-caption {
  font-size: 12px;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.model-select {
  width: 180px;
}

.expand-btn {
  align-self: flex-start;
  flex-shrink: 0;
  padding: var(--sp-1) var(--sp-2);
  font-family: inherit;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}

.expand-btn:hover {
  border-color: var(--brand-primary);
  color: var(--brand-hover);
}

.expand-btn:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}

/* 面板外壳：以边框建立层级，不堆阴影 */
.panel {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.chat-panel {
  flex: 1;
  min-width: 480px;
}

.chat-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  scroll-behavior: smooth;
  display: flex;
  flex-direction: column;
}

.chat-scroll::-webkit-scrollbar {
  width: 6px;
}

.chat-scroll::-webkit-scrollbar-track {
  background: transparent;
}

.chat-scroll::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: var(--radius);
}

.chat-scroll::-webkit-scrollbar-thumb:hover {
  background: var(--text-tertiary);
}

.chat-state {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  padding: var(--sp-6);
}

.chat-state > * {
  width: 100%;
  max-width: 520px;
}

/* 响应式：窄屏自动收起右栏，避免挤压聊天主区 */
@media (max-width: 1100px) {
  .expand-btn.right {
    display: none;
  }
  .chat-panel {
    min-width: 0;
  }
}

@media (max-width: 768px) {
  .console-page {
    padding: var(--sp-2);
    gap: var(--sp-2);
  }
  .console-body {
    position: relative;
    gap: var(--sp-2);
  }
  /* 会话栏改为浮层：覆盖在聊天区左上角，选择/新建后自动收起 */
  .console-body :deep(.sessions-panel) {
    position: absolute;
    top: 0;
    left: 0;
    bottom: 0;
    width: min(260px, 72vw);
    z-index: 20;
    box-shadow: var(--shadow-lg);
  }
  .expand-btn.left {
    display: none;
  }
  .session-toggle {
    display: inline-flex;
  }
  .model-select {
    width: 140px;
  }
}
</style>
