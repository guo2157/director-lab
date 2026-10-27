<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { NButton, NEmpty, NInput, NPopconfirm, NSelect, NSpin, NTag } from 'naive-ui'
import { ApiError, get, post } from '@/api/client'
import {
  archiveSession,
  createSession,
  listMessages,
  listSessions,
  updateSession,
} from '@/api/agent'
import type { AgentMessage, AgentSession } from '@/api/agent'
import { listJobs } from '@/api/jobs'

const message = useMessage()
const router = useRouter()

// --- 面板折叠 ---
const sessionsCollapsed = ref(false)
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
  } catch (e) {
    message.error(`创建会话失败: ${e instanceof Error ? e.message : String(e)}`)
  }
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
    if (activeSessionId.value === s.id) activeSessionId.value = sessions.value[0]?.id ?? null
    message.success('会话已归档')
  } catch (e) {
    message.error(`归档失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

// --- 中栏：对话与执行主区 ---
const messages = ref<AgentMessage[]>([])

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

async function send() {
  const content = inputText.value.trim()
  if (!content || !activeSessionId.value || sending.value) return
  sending.value = true
  inputText.value = ''
  try {
    const chatRes = await post<{
      user_message: AgentMessage
      assistant_message: AgentMessage
    }>(`/agent/sessions/${activeSessionId.value}/chat`, { content })
    messages.value.push(chatRes.user_message, chatRes.assistant_message)
    scrollToBottom(true)
    await loadSessions()
    await loadContext()
  } catch (e) {
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
interface JobBrief {
  id: string
  job_type: string
  status: string
  error_code?: string | null
  created_at?: string | null
}

const projects = ref<ProjectBrief[]>([])
const contextJobs = ref<JobBrief[]>([])
const contextLoading = ref(false)
const linkingMode = ref(false)
const linkTarget = ref<string | null>(null)

const linkedProject = computed(
  () => projects.value.find((p) => p.id === activeSession.value?.project_id) ?? null,
)

const JOB_STATUS_TEXT: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  lost: '丢失',
}

function jobStatusText(s: string): string {
  return JOB_STATUS_TEXT[s] ?? s
}

function jobStatusType(s: string): 'default' | 'success' | 'error' | 'warning' | 'info' {
  if (s === 'succeeded') return 'success'
  if (s === 'failed' || s === 'lost') return 'error'
  if (s === 'running') return 'info'
  if (s === 'queued') return 'warning'
  return 'default'
}

async function loadProjects() {
  try {
    const res = await get<{ items: ProjectBrief[] }>('/projects', { page_size: 50 })
    projects.value = res.items
  } catch (e) {
    message.error(`加载项目失败: ${e instanceof Error ? e.message : String(e)}`)
  }
}

async function loadContext() {
  const pid = activeSession.value?.project_id
  contextJobs.value = []
  if (!pid) return
  contextLoading.value = true
  try {
    const res = await listJobs({ project_id: pid, page_size: 8 })
    contextJobs.value = (res.items as unknown as JobBrief[]) ?? []
  } catch {
    contextJobs.value = []
  } finally {
    contextLoading.value = false
  }
}

watch(
  () => activeSession.value?.project_id,
  () => void loadContext(),
)

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
            @click="activeSessionId = s.id"
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
                  <NButton size="tiny" quaternary type="error" class="archive-btn" @click.stop>归档</NButton>
                </template>
                归档后消息保留，可恢复。确认归档？
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
                v-if="m.role === 'assistant' && assetRefsOf(m).length"
                class="asset-thumbs"
              >
                <template v-for="a in assetRefsOf(m)" :key="a.asset_id">
                  <a
                    v-if="isImageAsset(a.asset_type)"
                    class="asset-thumb"
                    :href="`/api/assets/${a.asset_id}/file`"
                    target="_blank"
                    rel="noopener"
                    :title="`${a.name}（${a.asset_type}）`"
                  >
                    <img
                      :src="`/api/assets/${a.asset_id}/file?thumb=true`"
                      :alt="a.name"
                      loading="lazy"
                    />
                    <span class="asset-thumb-label">{{ a.asset_type }}</span>
                  </a>
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
            </div>
          </div>
        </div>
      </div>
      <div class="chat-input">
        <NInput
          v-model:value="inputText"
          type="textarea"
          :rows="2"
          placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
          :disabled="!activeSessionId || sending"
          @keydown.enter.exact.prevent="send"
        />
        <NButton
          type="primary"
          :loading="sending"
          :disabled="!inputText.trim() || !activeSessionId"
          @click="send"
        >
          发送
        </NButton>
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
          </div>

          <div class="ctx-label">最近任务</div>
          <NSpin :show="contextLoading" size="small">
            <div v-if="!contextJobs.length" class="ctx-empty">暂无任务记录</div>
            <div v-else class="job-list">
              <div v-for="j in contextJobs" :key="j.id" class="job-item">
                <span class="job-type">{{ j.job_type }}</span>
                <NTag size="tiny" :type="jobStatusType(j.status)">{{ jobStatusText(j.status) }}</NTag>
                <span v-if="j.status === 'failed' && j.error_code" class="job-error" :title="j.error_code">
                  {{ j.error_code }}
                </span>
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
  gap: 10px;
  height: 100%;
  min-height: 0;
  padding: 10px;
  max-width: 1600px;
  margin: 0 auto;
  box-sizing: border-box;
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
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
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
}
.chat-state {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}
.message-list {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.message-row {
  display: flex;
}
.message-row.user {
  justify-content: flex-end;
}
.message-row.user .message-bubble {
  background: rgba(99, 102, 241, 0.08);
  border-color: rgba(99, 102, 241, 0.25);
}
.message-bubble {
  max-width: 76%;
  padding: 10px 14px;
  border-radius: 12px;
  border: 1px solid var(--border-subtle, #e5e7eb);
  background: var(--bg-elevated, #f9f9f9);
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
  line-height: 1.6;
}
/* 气泡内资产缩略图（agent asset_refs） */
.asset-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
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
.chat-input {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  border-top: 1px solid var(--border-subtle, #e5e7eb);
  align-items: flex-end;
  flex-shrink: 0;
}

/* 右栏 */
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
.job-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.job-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  min-width: 0;
}
.job-type {
  color: var(--text-primary, #111);
  flex-shrink: 0;
}
.job-error {
  color: var(--text-tertiary, #999);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
  .sessions-panel {
    width: 200px;
  }
  .model-select {
    width: 130px;
  }
}
</style>
