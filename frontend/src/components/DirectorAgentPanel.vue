<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { NButton, NEmpty, NInput, NSelect, NSpin, NTag } from 'naive-ui'
import { storeToRefs } from 'pinia'
import type { AgentApproval, AgentMessage } from '@/api/agent'
import { useAgentStore } from '@/stores/agent'
import { navigateFromPayload } from '@/services/workspaceNavigation'

const route = useRoute()
const router = useRouter()
const tip = useMessage()

/**
 * 状态全部来自 agent store（Phase 2）：面板收起/展开、跨页面切换都不再丢失
 * 会话与消息；组件只保留纯 UI 状态（展开态、审批折叠、滚动容器）。
 */
const agent = useAgentStore()
const {
  sessions,
  activeSessionId,
  messages,
  sessionsLoading,
  messagesLoading,
  sending,
  approvalBusyId,
  sessionOptions,
  pendingApprovals,
} = storeToRefs(agent)

/** 输入草稿同步到 store，收起面板后不丢 */
const inputText = computed({
  get: () => agent.draft,
  set: (v: string) => {
    agent.draft = v
  },
})

const scrollEl = ref<HTMLElement | null>(null)
const showApprovals = ref(false)

/**
 * 面板展开态由 store 持有：AppLayout 需要据此决定右侧是否让出空间，
 * 同时保证切换路由后展开状态不丢失。
 */
const expanded = computed(() => agent.panelOpen)

/** 拖拽面板左边缘调整宽度：面板贴右，向左拖拽即变宽 */
function onResizeStart(e: MouseEvent) {
  if (e.button !== 0) return
  e.preventDefault()
  const startX = e.clientX
  const startWidth = agent.panelWidth
  function onMove(ev: MouseEvent) {
    agent.setPanelWidth(startWidth - (ev.clientX - startX))
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

/** 视口变化时重新夹取宽度，避免窄屏下挤压主内容 */
function onViewportResize() {
  if (agent.panelOpen) agent.setPanelWidth(agent.panelWidth)
}

function scrollToBottom(smooth = false) {
  nextTick(() => {
    const el = scrollEl.value
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  })
}

async function switchSession(id: string | null) {
  await agent.switchSession(id)
  scrollToBottom()
}

async function newSession() {
  try {
    await agent.newSession(route.params.id as string | undefined)
    scrollToBottom()
  } catch (e) {
    tip.error(`新建会话失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

/**
 * 处理 assistant payload 中的导航指令。
 * 统一走 services/workspaceNavigation.ts：白名单校验 → 参数校验 → 跳转 → 上报。
 * 跳转失败时不谎报成功，如实提示用户。
 */
async function handleNavigation(msg: AgentMessage) {
  const payload = msg.payload as Record<string, unknown> | null
  if (!payload || payload.route_action !== 'navigate_workspace') return
  const projectId = route.params.id as string | undefined
  const result = await navigateFromPayload(router, payload, projectId)
  if (result && !result.ok) {
    agent.setNavNotice(result.message)
    tip.warning(result.message)
  }
}

async function send() {
  const content = inputText.value.trim()
  if (!content || !activeSessionId.value || sending.value) return
  try {
    const reply = await agent.sendMessage(content)
    scrollToBottom(true)
    if (reply) await handleNavigation(reply)
  } catch (e) {
    tip.error(`发送失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

async function decideApproval(a: AgentApproval, approved: boolean) {
  try {
    await agent.decideApproval(a, approved)
    tip.success(approved ? '已批准' : '已拒绝')
  } catch (e) {
    tip.error(`操作失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

async function runApproved(a: AgentApproval) {
  try {
    await agent.runApproved(a)
    tip.success('已提交执行')
  } catch (e) {
    tip.error(`执行失败：${e instanceof Error ? e.message : String(e)}`)
  }
}

function approvalLabel(a: AgentApproval) {
  const name = a.tool_name || '待审批操作'
  const taskId = (a as unknown as { task_id?: string }).task_id
  return taskId ? name + ' · ' + taskId.slice(0, 8) : name
}

function roleLabel(role: string) {
  if (role === 'user') return '我'
  if (role === 'assistant') return '助手'
  return role
}

onMounted(async () => {
  // store 为全局单例：已有会话数据时不重复请求，避免多余往返
  if (!sessions.value.length) {
    await agent.loadSessions()
  }
  if (activeSessionId.value && !messages.value.length) {
    await agent.switchSession(activeSessionId.value)
  }
  scrollToBottom()
  window.addEventListener('resize', onViewportResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onViewportResize)
})
</script>

<template>
  <!-- 悬浮入口：始终固定在右下角，未展开时醒目展示 -->
  <button
    v-if="!expanded"
    type="button"
    class="agent-fab"
    title="AI 助手"
    @click="agent.openPanel()"
  >
    <svg class="agent-fab-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
    <span class="agent-fab-label">AI 助手</span>
  </button>

  <!-- 展开态：参与文档流的右侧面板，占据自身宽度而非覆盖主内容 -->
  <aside
    v-else
    class="agent-drawer"
    :style="{ width: agent.panelWidth + 'px' }"
  >
    <!-- 左边缘拖拽条：调整面板宽度 -->
    <div class="agent-resizer" title="拖拽调整宽度" @mousedown="onResizeStart" />
    <header class="agent-head">
      <div class="agent-head-main">
        <NSelect
          :value="activeSessionId"
          :options="sessionOptions"
          :loading="sessionsLoading"
          size="small"
          placeholder="选择会话"
          class="agent-session-select"
          @update:value="switchSession"
        />
        <NButton size="tiny" quaternary title="新建会话" @click="newSession">+</NButton>
      </div>
      <button type="button" class="agent-collapse" title="收起助手" @click="agent.closePanel()">×</button>
    </header>

    <div ref="scrollEl" class="agent-stream">
      <div v-if="messagesLoading" class="agent-state"><NSpin size="small" /></div>
      <NEmpty
        v-else-if="!activeSessionId"
        size="small"
        description="新建一个会话开始"
        class="agent-state"
      />
      <NEmpty
        v-else-if="!messages.length"
        size="small"
        description="说说你想做什么"
        class="agent-state"
      />
      <div v-else class="agent-messages">
        <article v-for="m in messages" :key="m.id" class="agent-msg" :class="m.role">
          <div class="agent-msg-role">{{ roleLabel(m.role) }}</div>
          <div class="agent-msg-body">{{ m.content }}</div>
        </article>
      </div>
    </div>

    <section v-if="pendingApprovals.length" class="agent-approvals">
      <button type="button" class="agent-approvals-toggle" @click="showApprovals = !showApprovals">
        待审批 {{ pendingApprovals.length }} 项
        <span class="chev">{{ showApprovals ? '▾' : '▸' }}</span>
      </button>
      <div v-show="showApprovals" class="agent-approval-list">
        <div v-for="a in pendingApprovals" :key="a.confirmation_id" class="agent-approval">
          <div class="agent-approval-title" :title="approvalLabel(a)">{{ approvalLabel(a) }}</div>
          <div class="agent-approval-actions">
            <template v-if="a.status === 'pending'">
              <NButton size="tiny" type="primary" :loading="approvalBusyId === a.confirmation_id" @click="decideApproval(a, true)">批准</NButton>
              <NButton size="tiny" :loading="approvalBusyId === a.confirmation_id" @click="decideApproval(a, false)">拒绝</NButton>
            </template>
            <template v-else>
              <NTag size="tiny" type="success">已批准</NTag>
              <NButton size="tiny" type="primary" :loading="approvalBusyId === a.confirmation_id" @click="runApproved(a)">执行</NButton>
            </template>
          </div>
        </div>
      </div>
    </section>

    <footer class="agent-composer">
      <NInput
        v-model:value="inputText"
        type="textarea"
        size="small"
        :autosize="{ minRows: 2, maxRows: 6 }"
        placeholder="描述你想做的事，或说『去分镜页』"
        :disabled="!activeSessionId || sending"
        @keydown.enter.exact.prevent="send"
      />
      <div class="agent-composer-bar">
        <span class="agent-hint">Enter 发送 · Shift+Enter 换行</span>
        <NButton size="small" type="primary" :loading="sending" :disabled="!inputText.trim() || !activeSessionId" @click="send">发送</NButton>
      </div>
    </footer>
  </aside>
</template>

<style scoped>
/* ── 悬浮按钮 ── */
.agent-fab {
  position: fixed;
  right: 20px;
  bottom: 24px;
  z-index: 200;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 18px;
  background: var(--brand-primary);
  color: var(--text-on-brand);
  border: 0;
  border-radius: 28px;
  cursor: pointer;
  box-shadow: 0 4px 18px var(--brand-line);
  font-family: var(--font-ui);
  font-size: 14px;
  font-weight: 600;
  transition: transform 0.18s var(--ease-out), box-shadow 0.18s var(--ease-out);
}

.agent-fab:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 22px var(--brand-line);
}

.agent-fab:active {
  transform: translateY(0);
}

.agent-fab-icon {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
}

.agent-fab-label {
  white-space: nowrap;
}

/* ── 展开面板（flex 子项，不覆盖主内容） ── */
.agent-drawer {
  position: relative;
  flex: 0 0 auto;
  flex-shrink: 0;
  height: 100%;
  z-index: 5;
  display: flex;
  flex-direction: column;
  width: 392px;
  min-width: 300px;
  max-width: min(760px, 46vw);
  background: var(--bg-panel);
  border-left: 1px solid var(--border-subtle);
  overflow: hidden;
}
/* 左边缘拖拽条 */
.agent-resizer {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 5px;
  cursor: col-resize;
  background: transparent;
  transition: background var(--transition-fast);
  z-index: 2;
}
.agent-resizer:hover,
.agent-resizer:active {
  background: var(--brand-primary);
}

/* ── 页头 ── */
.agent-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.agent-head-main {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.agent-session-select {
  flex: 1;
  min-width: 0;
}

.agent-collapse {
  appearance: none;
  background: none;
  border: 0;
  padding: 2px 8px;
  font-size: 20px;
  line-height: 1;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: color var(--transition-fast);
}

.agent-collapse:hover {
  color: var(--text-primary);
}

/* ── 消息流 ── */
.agent-stream {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.agent-stream::-webkit-scrollbar {
  width: 5px;
}

.agent-stream::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: 0;
}

.agent-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-6) var(--sp-4);
}

.agent-messages {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  padding: var(--sp-4) var(--sp-4) var(--sp-6);
}

.agent-msg {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.agent-msg-role {
  font-family: var(--font-ui);
  font-size: 11px;
  letter-spacing: 0.1em;
  color: var(--text-tertiary);
}

.agent-msg-body {
  font-family: var(--font-display);
  font-size: 14px;
  line-height: 1.85;
  color: var(--text-primary);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.agent-msg.user .agent-msg-body {
  color: var(--text-secondary);
  padding-left: var(--sp-3);
  border-left: 2px solid var(--border-strong);
}

.agent-msg.assistant .agent-msg-body {
  color: var(--text-primary);
}

/* ── 审批 ── */
.agent-approvals {
  flex-shrink: 0;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-sunken);
}

.agent-approvals-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  appearance: none;
  background: none;
  border: 0;
  padding: var(--sp-2) var(--sp-3);
  font-family: var(--font-ui);
  font-size: 12px;
  color: var(--accent-amber);
  cursor: pointer;
}

.agent-approvals-toggle .chev {
  color: var(--text-tertiary);
}

.agent-approval-list {
  padding: 0 var(--sp-3) var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-height: 200px;
  overflow-y: auto;
}

.agent-approval {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
}

.agent-approval-title {
  font-family: var(--font-ui);
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-approval-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* ── 输入框 ── */
.agent-composer {
  flex-shrink: 0;
  border-top: 1px solid var(--border-subtle);
  padding: var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.agent-composer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}

.agent-hint {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── 响应式 ── */
@media (max-width: 1100px) {
  .agent-drawer {
    min-width: 280px;
    max-width: min(600px, 52vw);
  }
}

@media (max-width: 768px) {
  .agent-fab {
    right: 12px;
    bottom: 16px;
    padding: 10px 14px;
    font-size: 13px;
  }

  .agent-fab-label {
    display: none;
  }

  /* 窄屏改为全屏浮层，避免挤压主内容 */
  .agent-drawer {
    position: fixed;
    inset: var(--app-header-height) 0 0 0;
    width: 100% !important;
    max-width: none;
    min-width: 0;
    z-index: 150;
    border-left: 0;
    box-shadow: var(--shadow-lg);
  }
  .agent-resizer {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-fab {
    transition: none;
  }
  .agent-collapse {
    transition: none;
  }
}
</style>
