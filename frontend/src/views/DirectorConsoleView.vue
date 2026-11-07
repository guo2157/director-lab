<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { NButton, NEmpty, NImage, NInput, NPopconfirm, NSelect, NSpin, NTag } from 'naive-ui'
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
  updateSession,
} from '@/api/agent'
import type { ReviewCard } from '@/api/agent'
import type { AgentApproval, AgentMessage, AgentSession } from '@/api/agent'
import { getProjectStyleConfig, getStyleOptions } from '@/api/projects'
import type { StyleConfig, StyleOptions } from '@/types/project'
import TaskCard from '@/components/TaskCard.vue'
import { useTaskEvents } from '@/composables/useTaskEvents'
import type { TaskCardData } from '@/api/tasks'

const message = useMessage()
const router = useRouter()

// --- 面板折叠 ---
const sessionsCollapsed = ref(typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches)
const contextCollapsed = ref(false)

// --- 左栏：历史会话导航（唯一职责：找到/切换/管理会话） ---
const sessions = ref<AgentSession[]>([])
const sessionsLoading = ref(false)
const activeSessionId = ref<string | null>(null)
const searchKeyword = ref('')
const activeSession = computed(
  () => sessions.value.find((s) => s.id === activeSessionId.value) ?? null,
)

const filteredSessions = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return sessions.value
  return sessions.value.filter((s) => s.title.toLowerCase().includes(kw))
})

async function loadSessions() {
  sessionsLoading.value = true
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
    message.error(`加载会话列表失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    sessionsLoading.value = false
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
function selectSession(s: { id: string }) {
  activeSessionId.value = s.id
  if (window.matchMedia('(max-width: 768px)').matches) sessionsCollapsed.value = true
}

const editingId = ref<string | null>(null)
const editingTitle = ref('')

function renameSession(s: AgentSession) {
  editingId.value = s.id
  editingTitle.value = s.title
}

async function confirmRename() {
  if (!editingId.value) return
  const title = editingTitle.value.trim()
  if (!title) {
    editingId.value = null
    return
  }
  try {
    const updated = await updateSession(editingId.value, { title })
    const idx = sessions.value.findIndex((s) => s.id === updated.id)
    if (idx >= 0) sessions.value[idx] = updated
  } catch (e) {
    message.error(`重命名失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    editingId.value = null
  }
}

async function removeSession(s: AgentSession) {
  try {
    await archiveSession(s.id)
    await loadSessions()
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

// --- 统一任务卡片：消费 assistant payload.task_card + /api/tasks/events SSE（单卡片动态更新，不刷屏） ---
const { taskSnapshots, registerCard, connect: connectTaskEvents } = useTaskEvents()
/** 消息 payload 中的任务卡片（覆盖 parse/generate/compose/export） */
function taskCardOf(m: AgentMessage): TaskCardData | null {
  if (m.role !== 'assistant') return null
  const p = m.payload as { task_card?: TaskCardData } | null
  return p?.task_card ?? null
}
/** 搜索卡片的实时任务快照（payload.search.task_id → SSE 快照，进行中实时更新） */
const SEARCH_PHASE_TEXT: Record<string, string> = {
  searching: '搜索中',
  validating: '验证结果',
  fetching: '读取正文',
  summarizing: '汇总结果',
}
function searchSnapshotOf(m: AgentMessage): { status?: string; progress?: number; phase?: string } | null {
  const rec = searchRecordOf(m)
  const tid = (rec as unknown as { task_id?: string } | null)?.task_id
  if (!tid) return null
  const snap = taskSnapshots[tid] as { status?: string; progress?: number; phase?: string } | undefined
  return snap ?? null
}
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
})

/** 气泡内可展示的资产引用（由后端 assistant payload.asset_refs 提供） */
interface AssetRef {
  asset_id: string
  asset_type: string
  name: string
  has_thumb: boolean
  validity: string
}
function assetRefsOf(m: AgentMessage): AssetRef[] {
  const p = m.payload as { asset_refs?: AssetRef[] } | null
  return Array.isArray(p?.asset_refs) ? (p.asset_refs as AssetRef[]) : []
}
/** 视频类资产无法用 img 直出，降级为链接卡片 */
function isImageAsset(t: string): boolean {
  return !['clip', 'video'].includes(t)
}
/** 缩略图加载失败的资产（历史消息中资产可能已失效或被清理，失败即隐藏避免破图） */
const failedAssetIds = ref<Set<string>>(new Set())
function onThumbError(assetId: string): void {
  const next = new Set(failedAssetIds.value)
  next.add(assetId)
  failedAssetIds.value = next
}
function visibleAssetRefs(m: AgentMessage): AssetRef[] {
  return assetRefsOf(m).filter((a) => !failedAssetIds.value.has(a.asset_id))
}

/** 联网搜索执行记录（由后端 assistant payload.search 提供，真实执行数据非 LLM 生成） */
interface SearchRecord {
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
function searchRecordOf(m: AgentMessage): SearchRecord | null {
  const p = m.payload as { search?: SearchRecord } | null
  return p?.search ?? null
}
const SEARCH_STATUS_TEXT: Record<string, string> = {
  success: '搜索完成',
  partial: '部分成功（部分来源读取失败）',
  empty: '无有效结果',
  failed: '搜索失败',
}
const messagesLoading = ref(false)
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
  try {
    const res = await listMessages(sessionId, { page_size: 200 })
    messages.value = res.items
    scrollToBottom(true)
  } catch (e) {
    message.error(`加载消息失败: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    messagesLoading.value = false
  }
}

watch(activeSessionId, (id) => {
  messages.value = []
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
interface ProjectBrief {
  id: string
  name: string
  status: string
  current_stage: string
  shot_total: number
  shot_done: number
}
const projects = ref<ProjectBrief[]>([])
// 生成类操作的待审批申请（批准与执行分离：批准只放行，执行才建任务）
const approvals = ref<AgentApproval[]>([])
const approvalLoading = ref(false)
const approvingId = ref<string | null>(null)

// ── 统一审核卡片（剧本草稿/驳回/修改/通过，按版本分组） ──
const reviewCards = ref<ReviewCard[]>([])
const reviewCardsLoading = ref(false)

/** 审核状态中文标签 */
const REVIEW_STATUS_LABEL: Record<string, string> = {
  pending: '待审核',
  approved: '已通过',
  rejected: '已驳回',
  revision_requested: '修改中',
  cancelled: '已取消',
  deferred: '已暂缓',
  expired: '已过期',
}

/** 审核状态标签色（匹配 NTag type） */
const REVIEW_STATUS_TYPE: Record<string, 'default' | 'success' | 'warning' | 'error' | 'info'> = {
  pending: 'warning',
  approved: 'success',
  rejected: 'error',
  revision_requested: 'info',
  cancelled: 'default',
  deferred: 'default',
  expired: 'default',
}

/** 审核对象中文名称 */
function reviewTargetLabel(r: ReviewCard): string {
  const base = r.target_type === 'script' ? '剧本' : r.target_type ?? '对象'
  const v = r.target_version
  if (!v) return `${base}草稿`
  return `${base}第 ${v} 版`
}

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

/** 审核卡片：按版本排序（最新在前），同一版本内按轮次排序 */
const sortedReviewCards = computed(() =>
  [...reviewCards.value].sort((a, b) => {
    const va = a.target_version ?? -1
    const vb = b.target_version ?? -1
    if (va !== vb) return vb - va
    return (b.round ?? 0) - (a.round ?? 0)
  }),
)
const linkingMode = ref(false)
const linkTarget = ref<string | null>(null)

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

/** 把后端下发的枚举值映射为中文展示名，前端不硬编码枚举 */
function labelOf(
  list: Array<{ value: string; label: string }> | undefined,
  value: string | undefined,
): string {
  if (!value) return '未设置'
  return list?.find((i) => i.value === value)?.label ?? value
}

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

// 面板只展示关键参数（目标类型 + 镜头），避免把整段入参铺满侧栏
function approvalSummary(a: AgentApproval): string {
  const args = (a.args ?? {}) as Record<string, unknown>
  const target = String(args.target ?? '')
  const shot = String(args.shot_id ?? '')
  return [target || '生成', shot].filter(Boolean).join(' · ')
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
    linkingMode.value = false
    linkTarget.value = null
    return
  }
  try {
    const updated = await updateSession(activeSession.value.id, { project_id: pid })
    const idx = sessions.value.findIndex((s) => s.id === updated.id)
    if (idx >= 0) sessions.value[idx] = updated
    linkingMode.value = false
    linkTarget.value = null
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

function fmtTime(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

onMounted(() => {
  void loadSessions()
  void loadProjects()
  void loadModels()
})
</script>

<template>
  <div class="director-console">
    <!-- 左栏：会话导航 -->
    <aside v-if="!sessionsCollapsed" class="panel sessions-panel">
      <div class="panel-header">
        <span class="panel-title">会话</span>
        <div class="header-actions">
          <NButton size="tiny" quaternary @click="sessionsCollapsed = true">收起</NButton>
          <NButton size="tiny" type="primary" @click="newSession">新建</NButton>
        </div>
      </div>
      <div class="search-box">
        <NInput v-model:value="searchKeyword" size="small" placeholder="搜索会话" clearable />
      </div>
      <NSpin :show="sessionsLoading" size="small" class="session-spin">
        <div class="session-list">
          <NEmpty v-if="!filteredSessions.length" size="small" description="暂无会话" class="list-empty" />
          <div
            v-for="s in filteredSessions"
            :key="s.id"
            class="session-item"
            :class="{ active: s.id === activeSessionId }"
            @click="selectSession(s)"
          >
            <template v-if="editingId === s.id">
              <NInput
                v-model:value="editingTitle"
                size="tiny"
                autofocus
                @keyup.enter="confirmRename"
                @blur="confirmRename"
              />
            </template>
            <template v-else>
              <div class="session-info">
                <div class="session-title" :title="s.title" @dblclick="renameSession(s)">
                  {{ s.title }}
                </div>
                <div class="session-meta">
                  <span class="session-time">{{ fmtTime(s.last_message_at ?? s.updated_at) }}</span>
                </div>
              </div>
              <NPopconfirm @positive-click="removeSession(s)">
                <template #trigger>
                  <NButton size="tiny" quaternary type="error" class="archive-btn" @click.stop>删除</NButton>
                </template>
                删除后该会话将从列表移除。确认删除？
              </NPopconfirm>
            </template>
          </div>
        </div>
      </NSpin>
    </aside>
    <button v-else class="expand-btn left" @click="sessionsCollapsed = false">会话 ›</button>

    <!-- 中栏：对话与执行主区 -->
    <section class="panel chat-panel">
      <div class="panel-header">
        <span class="panel-title chat-title">{{ activeSession?.title ?? '导演台' }}</span>
        <div class="header-actions">
          <NButton size="small" quaternary class="session-toggle" @click="sessionsCollapsed = false">☰ 会话</NButton>
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
        </div>
      </div>
      <div ref="scrollEl" class="chat-scroll" @scroll="onScroll">
        <div v-if="messagesLoading" class="chat-state"><NSpin size="small" /></div>
        <NEmpty
          v-else-if="!activeSessionId"
          description="选择左侧会话或新建会话开始对话"
          class="chat-state"
        />
        <NEmpty v-else-if="!messages.length" description="发送第一条消息开始对话" class="chat-state" />
        <div v-else class="message-list">
          <div v-for="m in messages" :key="m.id" class="message-row" :class="m.role">
            <div class="message-bubble">
              <div class="message-role">
                {{ m.role === 'user' ? '我' : m.role === 'assistant' ? '导演台助手' : m.role }}
              </div>
              <div class="message-content">{{ m.content }}</div>
              <div
                v-if="m.role === 'assistant' && visibleAssetRefs(m).length"
                class="asset-thumbs"
              >
                <template v-for="a in visibleAssetRefs(m)" :key="a.asset_id">
                  <!-- 图片类：气泡内用低成本缩略图直出，点击进入预览层查看原图
                       （可缩放/旋转/下载），避免新开标签页直接加载数 MB PNG -->
                  <div
                    v-if="isImageAsset(a.asset_type)"
                    class="asset-thumb"
                    :title="`${a.name}（${a.asset_type}）`"
                  >
                    <NImage
                      :src="`/api/assets/${a.asset_id}/file?thumb=true`"
                      :preview-src="`/api/assets/${a.asset_id}/file`"
                      :img-props="{ alt: a.name, loading: 'lazy' }"
                      width="100%"
                      height="100%"
                      object-fit="cover"
                      @error="onThumbError(a.asset_id)"
                    />
                    <span class="asset-thumb-label">{{ a.asset_type }}</span>
                  </div>
                  <a
                    v-else
                    class="asset-thumb-link"
                    :href="`/api/assets/${a.asset_id}/file`"
                    target="_blank"
                    rel="noopener"
                    :title="`${a.name}（${a.asset_type}）`"
                  >
                    ▶ {{ a.asset_type }}
                  </a>
                </template>
              </div>
              <!-- 联网搜索执行卡片：展示真实搜索执行记录（来自后端 payload.search，非 LLM 生成） -->
              <div
                v-if="m.role === 'assistant' && searchRecordOf(m)"
                class="search-card"
              >
                <div class="search-card-header">
                  <span class="search-card-icon">🔍</span>
                  <span class="search-card-title">联网搜索</span>
                  <span class="search-card-status" :data-status="searchRecordOf(m)!.status">
                    {{ SEARCH_STATUS_TEXT[searchRecordOf(m)!.status] || searchRecordOf(m)!.status }}
                  </span>
                </div>
                <!-- 搜索进行中：SSE 快照实时展示阶段与进度（终态后隐藏，回落到最终记录） -->
                <div
                  v-if="searchSnapshotOf(m) && searchSnapshotOf(m)!.status === 'running'"
                  class="search-card-live"
                >
                  <span class="search-card-live-phase">{{
                    SEARCH_PHASE_TEXT[searchSnapshotOf(m)!.phase ?? ''] || '执行中'
                  }}</span>
                  <div class="search-card-live-bar">
                    <div
                      class="search-card-live-fill"
                      :style="{ width: `${Math.max(0, Math.min(100, searchSnapshotOf(m)!.progress ?? 0))}%` }"
                    />
                  </div>
                  <span class="search-card-live-pct">{{ Math.max(0, Math.min(100, searchSnapshotOf(m)!.progress ?? 0)) }}%</span>
                </div>
                <div class="search-card-meta">
                  <span>关键词：{{ searchRecordOf(m)!.query }}</span>
                  <span v-if="searchRecordOf(m)!.provider">引擎：{{ searchRecordOf(m)!.provider }}</span>
                  <span>有效结果：{{ searchRecordOf(m)!.valid_result_count }}/{{ searchRecordOf(m)!.result_count }}</span>
                  <span v-if="searchRecordOf(m)!.duration_ms != null">耗时：{{ searchRecordOf(m)!.duration_ms }}ms</span>
                </div>
                <div v-if="searchRecordOf(m)!.error" class="search-card-error">
                  {{ searchRecordOf(m)!.error }}
                </div>
              </div>
              <!-- 任务卡片：消费 payload.task_card，SSE 事件驱动单卡片动态更新 -->
              <TaskCard
                v-if="m.role === 'assistant' && taskCardOf(m)"
                :card="taskCardOf(m)!"
                :snapshot="taskSnapshots[taskCardOf(m)!.task_id] ?? null"
              />
            </div>
          </div>
          <!-- 思考过程气泡：等待响应期间分阶段展示，响应到达后由最终回复替换 -->
          <div v-if="thinking" class="message-row assistant">
            <div class="message-bubble thinking-bubble">
              <div class="message-role">导演台助手</div>
              <div class="message-content thinking-content">
                {{ thinkingStage }}<span class="thinking-dots"><i></i><i></i><i></i></span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <!-- 审核卡片区：按版本分组，与消息流混合展示，最新在前 -->
      <div v-if="sortedReviewCards.length" class="review-cards-section">
        <div class="review-cards-label">📋 审核记录</div>
        <NSpin :show="reviewCardsLoading" size="small">
          <div v-for="r in sortedReviewCards" :key="r.confirmation_id" class="review-card">
            <div class="review-card-header">
              <span class="review-card-target">{{ reviewTargetLabel(r) }}</span>
              <NTag :type="REVIEW_STATUS_TYPE[r.status] || 'default'" size="small">
                {{ REVIEW_STATUS_LABEL[r.status] || r.status }}
              </NTag>
            </div>
            <div v-if="r.question" class="review-card-question">{{ r.question }}</div>
            <div class="review-card-meta">
              <span v-if="r.round != null">第 {{ r.round }} 轮审核</span>
              <span v-if="r.created_at">{{ fmtTime(r.created_at) }}</span>
              <span v-if="r.resolved_at">裁决于 {{ fmtTime(r.resolved_at) }}</span>
            </div>
            <div v-if="r.user_response" class="review-card-response">
              用户回复：{{ r.user_response }}
            </div>
            <div v-if="r.comment" class="review-card-comment">
              备注：{{ r.comment }}
            </div>
          </div>
        </NSpin>
      </div>
      <div class="chat-input">
        <div class="composer">
          <NInput
            v-model:value="inputText"
            type="textarea"
            :rows="2"
            placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
            :disabled="!activeSessionId || sending"
            @keydown.enter.exact.prevent="send"
          />
          <NButton
            class="composer-send"
            type="primary"
            :loading="sending"
            :disabled="!inputText.trim() || !activeSessionId"
            @click="send"
          >
            发送
          </NButton>
        </div>
      </div>
    </section>

    <!-- 右栏：当前会话的项目/任务上下文 -->
    <aside v-if="!contextCollapsed" class="panel context-panel">
      <div class="panel-header">
        <span class="panel-title">任务上下文</span>
        <NButton size="tiny" quaternary @click="contextCollapsed = true">收起</NButton>
      </div>
      <div class="context-body">
        <!-- 无关联项目：空状态 + 按需关联 -->
        <template v-if="!linkedProject">
          <NEmpty
            size="small"
            :description="activeSession ? '当前会话为普通对话，未关联项目' : '请先选择或新建一个会话'"
            class="list-empty"
          />
          <template v-if="linkingMode">
            <NSelect
              v-model:value="linkTarget"
              :options="projects.map((p) => ({ label: p.name, value: p.id }))"
              size="small"
              placeholder="选择项目"
              class="link-select"
            />
            <div class="link-actions">
              <NButton size="tiny" type="primary" :disabled="!linkTarget" @click="linkProject(linkTarget)">确认</NButton>
              <NButton size="tiny" @click="linkingMode = false">取消</NButton>
            </div>
          </template>
          <NButton
            v-else
            size="small"
            class="link-btn"
            :disabled="!activeSession"
            @click="linkingMode = true"
          >
            {{ activeSession ? '关联项目' : '请先选择会话' }}
          </NButton>
        </template>

        <!-- 已关联项目：项目概览 + 最近任务 + 跳转入口 -->
        <template v-else>
          <div class="ctx-project">
            <div class="ctx-project-name" :title="linkedProject.name">{{ linkedProject.name }}</div>
            <div class="ctx-project-meta">
              <NTag size="small" :type="linkedProject.status === 'active' ? 'success' : 'default'">
                {{ linkedProject.status === 'active' ? '进行中' : linkedProject.status }}
              </NTag>
              <span class="ctx-stage">{{ linkedProject.current_stage }}</span>
              <span class="ctx-shots">镜头 {{ linkedProject.shot_done }}/{{ linkedProject.shot_total }}</span>
            </div>
            <div class="ctx-links">
              <a @click="goProject('storyboard')">分镜看板</a>
              <a @click="goProject('assets')">资产库</a>
              <a @click="goProject('compose')">合成导出</a>
              <a class="muted" @click="linkProject(null)">取消关联</a>
            </div>
            <div class="ctx-style">
              <div class="ctx-style-head">
                <span>风格基线</span>
                <a class="style-edit" @click="goProject('script')">调整 →</a>
              </div>
              <NSpin :show="styleLoading" size="small">
                <div v-if="!styleConfig" class="ctx-empty">尚未读取风格配置</div>
                <div v-else class="style-lines">
                  <div class="style-line">
                    <span class="style-k">视觉风格</span>
                    <span class="style-v">{{ styleLabel || styleConfig.visual_style }}</span>
                  </div>
                  <div class="style-line">
                    <span class="style-k">剧本类型</span>
                    <span class="style-v">{{ styleConfig.genre_tags.length ? styleConfig.genre_tags.join('、') : '未设置' }}</span>
                  </div>
                  <div class="style-line">
                    <span class="style-k">剧情节奏</span>
                    <span class="style-v">{{ labelOf(styleOptions?.pacing, styleConfig.pacing) }}</span>
                  </div>
                  <div class="style-line">
                    <span class="style-k">主情绪</span>
                    <span class="style-v">{{ styleConfig.emotional_tone[0] ?? '未设置' }}</span>
                  </div>
                </div>
              </NSpin>
            </div>
          </div>

          <div class="ctx-label">待审批生成</div>
          <NSpin :show="approvalLoading" size="small">
            <div v-if="!approvals.length" class="ctx-empty">暂无待审批申请</div>
            <div v-else class="approval-list">
              <div v-for="a in approvals" :key="a.confirmation_id" class="approval-item">
                <div class="approval-main">
                  <div class="approval-title" :title="a.tool_name">{{ approvalSummary(a) }}</div>
                  <div class="approval-actions">
                    <template v-if="a.status === 'pending'">
                      <NButton
                        size="tiny"
                        type="primary"
                        :loading="approvingId === a.confirmation_id"
                        @click="decideApproval(a, true)"
                      >批准</NButton>
                      <NButton
                        size="tiny"
                        :loading="approvingId === a.confirmation_id"
                        @click="decideApproval(a, false)"
                      >拒绝</NButton>
                    </template>
                    <template v-else-if="a.status === 'approved'">
                      <NTag size="tiny" type="success">已批准</NTag>
                      <NButton
                        size="tiny"
                        type="primary"
                        :loading="approvingId === a.confirmation_id"
                        @click="runApproved(a)"
                      >执行</NButton>
                    </template>
                  </div>
                </div>
              </div>
            </div>
          </NSpin>
        </template>
      </div>
    </aside>
    <button v-else class="expand-btn right" @click="contextCollapsed = false">‹ 上下文</button>
  </div>
</template>

<style scoped>
.director-console {
  display: flex;
  gap: 12px;
  height: 100%;
  min-height: 0;
  padding: 12px;
  max-width: 1600px;
  margin: 0 auto;
  box-sizing: border-box;
}
.session-toggle {
  display: none;
}
.expand-btn {
  align-self: flex-start;
  flex-shrink: 0;
  border: 1px solid var(--border-subtle, #e5e7eb);
  background: var(--bg-panel, #fff);
  color: var(--text-secondary, #666);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.expand-btn:hover {
  border-color: var(--brand-primary, #6366f1);
  color: var(--brand-primary, #6366f1);
}
.panel {
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  box-shadow: 0 1px 6px rgba(0, 0, 0, 0.03);
}
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-subtle, #e5e7eb);
  flex-shrink: 0;
  gap: 8px;
  min-width: 0;
}
.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary, #111);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

/* 左栏 */
.sessions-panel {
  width: 260px;
  flex-shrink: 0;
}
.search-box {
  padding: 8px 10px 4px;
  flex-shrink: 0;
}
.session-spin {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.session-spin :deep(.n-spin-content) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
.session-list {
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 7px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.session-item:hover {
  background: var(--bg-elevated, #f5f5f5);
}
.session-item.active {
  background: rgba(99, 102, 241, 0.1);
}
.session-info {
  flex: 1;
  min-width: 0;
}
.session-title {
  font-size: 13px;
  color: var(--text-primary, #111);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-meta {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--text-tertiary, #999);
  margin-top: 2px;
}
.archive-btn {
  opacity: 0;
  flex-shrink: 0;
}
.session-item:hover .archive-btn {
  opacity: 1;
}

/* 中栏 */
.chat-panel {
  flex: 1;
  min-width: 520px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
.chat-title {
  font-size: 14px;
}
.model-select {
  width: 170px;
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
  width: 5px;
}
.chat-scroll::-webkit-scrollbar-track {
  background: transparent;
}
.chat-scroll::-webkit-scrollbar-thumb {
  background: rgba(0, 0, 0, 0.12);
  border-radius: 8px;
}
.chat-scroll::-webkit-scrollbar-thumb:hover {
  background: rgba(0, 0, 0, 0.22);
}
.chat-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}
.message-list {
  padding: 24px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  flex: 1;
  min-height: min-content;
}
.message-row {
  display: flex;
  animation: msg-fade-in 0.25s ease-out;
}
@keyframes msg-fade-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.thinking-bubble {
  border-style: dashed;
}
.thinking-content {
  display: flex;
  align-items: center;
  color: var(--text-secondary, #666);
}
.thinking-dots {
  display: inline-flex;
  gap: 3px;
  margin-left: 4px;
}
.thinking-dots i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
  animation: thinking-blink 1.2s infinite ease-in-out;
}
.thinking-dots i:nth-child(2) {
  animation-delay: 0.2s;
}
.thinking-dots i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes thinking-blink {
  0%, 80%, 100% {
    opacity: 0.25;
  }
  40% {
    opacity: 1;
  }
}
.message-row.user {
  justify-content: flex-end;
}
.message-row.user .message-bubble {
  background: linear-gradient(135deg, rgba(99, 102, 241, 0.12) 0%, rgba(139, 92, 246, 0.07) 100%);
  border-color: rgba(99, 102, 241, 0.28);
  border-radius: 16px 4px 16px 16px;
  box-shadow: 0 2px 10px rgba(99, 102, 241, 0.08);
}
.message-row.user .message-bubble:hover {
  box-shadow: 0 4px 18px rgba(99, 102, 241, 0.16);
  transform: translateY(-1px);
}
.message-bubble {
  max-width: 76%;
  padding: 14px 18px;
  border-radius: 4px 16px 16px 16px;
  border: 1px solid var(--border-subtle, #e5e7eb);
  background: var(--bg-elevated, #f9f9f9);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04), 0 1px 2px rgba(0, 0, 0, 0.02);
  transition: box-shadow 0.2s ease, transform 0.15s ease;
}
.message-bubble:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06), 0 1px 3px rgba(0, 0, 0, 0.04);
  transform: translateY(-1px);
}
.message-role {
  font-size: 11px;
  color: var(--text-tertiary, #999);
  margin-bottom: 4px;
}
.message-content {
  font-size: 14px;
  color: var(--text-primary, #111);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
}
.message-content :deep(a) {
  color: #6366f1;
  text-decoration: none;
  border-bottom: 1px solid rgba(99, 102, 241, 0.3);
  transition: border-color 0.15s;
}
.message-content :deep(a:hover) {
  border-bottom-color: #6366f1;
}
.message-content :deep(code) {
  background: rgba(0, 0, 0, 0.05);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
  font-family: 'SF Mono', 'Fira Code', monospace;
}
.message-content :deep(pre) {
  background: rgba(0, 0, 0, 0.04);
  padding: 12px 14px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 8px 0;
}
.message-content :deep(pre code) {
  background: none;
  padding: 0;
}
/* 气泡内资产缩略图（agent asset_refs） */
.asset-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

/* 联网搜索执行卡片（agent web_search payload.search） */
.search-card {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid rgba(128, 128, 128, 0.25);
  border-radius: 8px;
  background: rgba(128, 128, 128, 0.06);
  font-size: 12px;
}
.search-card-header {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.search-card-status {
  margin-left: auto;
  padding: 1px 8px;
  border-radius: 10px;
  font-weight: 500;
  font-size: 11px;
  background: rgba(24, 160, 88, 0.15);
  color: #18a058;
}
.search-card-status[data-status='partial'] {
  background: rgba(240, 160, 32, 0.15);
  color: #f0a020;
}
.search-card-status[data-status='failed'],
.search-card-status[data-status='empty'] {
  background: rgba(208, 48, 80, 0.12);
  color: #d03050;
}
.search-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-top: 6px;
  opacity: 0.85;
}
.search-card-error {
  margin-top: 6px;
  color: #d03050;
}
.search-card-live {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  font-size: 12px;
  color: #2080f0;
}
.search-card-live-phase {
  min-width: 56px;
}
.search-card-live-bar {
  flex: 1;
  height: 4px;
  border-radius: 2px;
  background: #e8e8ee;
  overflow: hidden;
}
.search-card-live-fill {
  height: 100%;
  border-radius: 2px;
  background: #2080f0;
  transition: width 0.3s ease;
}
.search-card-live-pct {
  font-variant-numeric: tabular-nums;
}
.asset-thumb {
  position: relative;
  display: block;
  width: 96px;
  height: 96px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid var(--border-subtle, #e5e7eb);
  background: #000;
  cursor: zoom-in;
}
.asset-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.asset-thumb-label {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  font-size: 10px;
  padding: 2px 4px;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  text-align: center;
}
.asset-thumb-link {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 96px;
  height: 96px;
  border-radius: 8px;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  font-size: 12px;
  color: var(--text-secondary, #666);
  text-decoration: none;
}
.approval-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.approval-item {
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 8px;
  padding: 8px 10px;
}

.approval-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.approval-title {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* ── 审核卡片区（剧本/资产草案审核记录，按版本分组，聊天区底部） ── */
.review-cards-section {
  margin: 0 24px 16px;
  padding: 14px 18px;
  background: rgba(99, 102, 241, 0.04);
  border: 1px solid rgba(99, 102, 241, 0.15);
  border-radius: 12px;
}
.review-cards-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--brand-primary, #6366f1);
  margin-bottom: 10px;
}
.review-card {
  padding: 10px 12px;
  margin-bottom: 8px;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 8px;
  font-size: 12px;
}
.review-card:last-child {
  margin-bottom: 0;
}
.review-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.review-card-target {
  font-weight: 600;
  color: var(--text-primary, #111);
}
.review-card-question {
  margin-top: 6px;
  color: var(--text-secondary, #666);
  line-height: 1.6;
}
.review-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-tertiary, #999);
}
.review-card-response {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-primary, #111);
  background: rgba(99, 102, 241, 0.06);
  padding: 4px 8px;
  border-radius: 4px;
}
.review-card-comment {
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-secondary, #666);
}

.chat-input {
  display: block;
  padding: 12px 16px 14px;
  border-top: 1px solid var(--border-subtle, #e5e7eb);
  flex-shrink: 0;
  background: var(--bg-panel, #fff);
}
/* 输入框与发送按钮合为一体：按钮内嵌在输入框右下角，不再单独占一行 */
.composer {
  position: relative;
  display: block;
}
.composer :deep(.n-input) {
  border-radius: 12px;
  transition: box-shadow 0.25s ease, border-color 0.25s ease;
}
.composer :deep(.n-input.n-input--focus) {
  box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
}
.composer :deep(.n-input .n-input__textarea) {
  font-size: 14px;
  line-height: 1.7;
}
/* 让输入文字避开右下角的发送按钮，避免被遮挡 */
.composer :deep(.n-input .n-input__textarea-el) {
  padding-right: 92px;
}
.composer-send {
  position: absolute;
  right: 8px;
  bottom: 8px;
  z-index: 2;
  border-radius: 9px !important;
  height: 32px !important;
  min-width: 58px !important;
  font-weight: 500;
  transition: all 0.2s ease;
}
.composer-send.n-button--primary-type {
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
  border: none;
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.25);
}
.composer-send.n-button--primary-type:hover {
  box-shadow: 0 4px 14px rgba(99, 102, 241, 0.35);
}
.composer-send.n-button--primary-type:active {
  transform: translateY(0) scale(0.97);
  box-shadow: 0 1px 3px rgba(99, 102, 241, 0.2);
}

/* 右栏 */
.ctx-style {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--border-subtle, #e5e7eb);
}
.ctx-style-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 6px;
}
.style-edit {
  font-size: 12px;
  font-weight: 500;
  color: var(--brand-primary, #6366f1);
  cursor: pointer;
}
.style-lines {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.style-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  line-height: 18px;
}
.style-k {
  flex: 0 0 56px;
  color: var(--text-secondary);
}
.style-v {
  flex: 1 1 auto;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.context-panel {
  width: 300px;
  flex-shrink: 0;
}
.context-body {
  padding: 12px 14px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ctx-project {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ctx-project-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary, #111);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ctx-project-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary, #666);
  flex-wrap: wrap;
}
.ctx-links {
  display: flex;
  gap: 12px;
  font-size: 12px;
  flex-wrap: wrap;
}
.ctx-links a {
  color: var(--brand-primary, #6366f1);
  cursor: pointer;
}
.ctx-links a:hover {
  text-decoration: underline;
}
.ctx-links a.muted {
  color: var(--text-tertiary, #999);
}
.ctx-label {
  font-size: 12px;
  color: var(--text-tertiary, #999);
  margin-top: 4px;
}
.ctx-empty {
  font-size: 12px;
  color: var(--text-tertiary, #999);
  padding: 8px 0;
}
.link-btn {
  align-self: flex-start;
}
.link-select {
  width: 100%;
}
.link-actions {
  display: flex;
  gap: 8px;
}
.list-empty {
  padding: 24px 0;
}

/* 响应式：窄屏自动收起右栏，避免挤压聊天主区 */
@media (max-width: 1100px) {
  .context-panel {
    width: 260px;
  }
}
@media (max-width: 960px) {
  .context-panel {
    display: none;
  }
  .expand-btn.right {
    display: none;
  }
  .chat-panel {
    min-width: 0;
  }
}
@media (max-width: 768px) {
  /* 会话栏改为浮层：覆盖在聊天区左上角，选择/新建后自动收起 */
  .director-console {
    position: relative;
  }
  .sessions-panel {
    position: absolute;
    top: 8px;
    left: 8px;
    bottom: 8px;
    width: min(260px, 72vw);
    z-index: 20;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18);
  }
  .sessions-panel .panel-title {
    display: none;
  }
  .sessions-panel .panel-header {
    justify-content: flex-end;
  }
  /* 手机端显示聊天头部的会话入口，隐藏左侧旧展开按钮 */
  .session-toggle {
    display: inline-flex;
  }
  .expand-btn.left {
    display: none;
  }
  .model-select {
    width: 130px;
  }
  .chat-input {
    padding: 10px 12px 12px;
  }
  .composer :deep(.n-input .n-input__textarea-el) {
    padding-right: 82px;
  }
  .message-bubble {
    max-width: 88%;
  }
  .director-console {
    padding: 8px;
    gap: 8px;
  }
}
</style>
