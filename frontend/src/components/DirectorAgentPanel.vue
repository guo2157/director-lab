<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { NButton, NIcon, NInput, NSelect, NSpin, NTag } from 'naive-ui'
import { AddOutline, CloseOutline, SparklesOutline } from '@vicons/ionicons5'
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

/** 空态引导：三条常用指令，点击即填入输入框（不直接发送，避免误触发写操作） */
const starterPrompts = [
  '这个项目现在到哪一步了',
  '带我去分镜图页',
  '把第 1 集的镜头生成排队',
]

function useStarter(text: string) {
  agent.draft = text
}

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
  window.addEventListener('keydown', onHotkey)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onViewportResize)
  window.removeEventListener('keydown', onHotkey)
})

/**
 * ⌘K / Ctrl+K 开关助手面板。
 * 在输入控件内不拦截，避免吃掉编辑器里的原生快捷键。
 */
function onHotkey(e: KeyboardEvent) {
  if (e.key.toLowerCase() !== 'k' || !(e.metaKey || e.ctrlKey)) return
  const el = e.target as HTMLElement | null
  if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return
  e.preventDefault()
  agent.togglePanel()
}
</script>

<template>
  <!-- 悬浮入口：右下角贴边停靠，低权重次级样式（深色面 + 发丝描边 + 品牌色图标） -->
  <button
    v-if="!expanded"
    type="button"
    class="agent-fab"
    aria-label="打开 AI 助手"
    title="AI 助手"
    @click="agent.openPanel()"
  >
    <NIcon class="agent-fab-icon" :component="SparklesOutline" :size="16" />
    <span class="agent-fab-label">AI 助手</span>
    <span class="agent-fab-key" aria-hidden="true">⌘K</span>
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
      <div class="agent-head-title">
        <NIcon :component="SparklesOutline" :size="15" class="agent-head-mark" />
        <span class="agent-head-label">AI 助手</span>
      </div>
      <div class="agent-head-actions">
        <button type="button" class="agent-icon-btn" aria-label="新建会话" title="新建会话" @click="newSession">
          <NIcon :component="AddOutline" :size="16" />
        </button>
        <button type="button" class="agent-icon-btn" aria-label="收起助手" title="收起助手" @click="agent.closePanel()">
          <NIcon :component="CloseOutline" :size="16" />
        </button>
      </div>
    </header>

    <!-- 会话切换独立成行，不再挤占标题行 -->
    <div class="agent-session-bar">
      <NSelect
        :value="activeSessionId"
        :options="sessionOptions"
        :loading="sessionsLoading"
        size="small"
        placeholder="选择会话"
        class="agent-session-select"
        @update:value="switchSession"
      />
    </div>

    <div ref="scrollEl" class="agent-stream">
      <div v-if="messagesLoading" class="agent-state"><NSpin size="small" /></div>
      <div v-else-if="!activeSessionId || !messages.length" class="agent-empty">
        <div class="agent-empty-mark" aria-hidden="true">
          <NIcon :component="SparklesOutline" :size="20" />
        </div>
        <p class="agent-empty-title">{{ activeSessionId ? '说说你想做什么' : '新建一个会话开始' }}</p>
        <p class="agent-empty-desc">助手可以读项目数据、切换页面、提交生成任务</p>
        <div class="agent-empty-actions">
          <button
            v-for="s in starterPrompts"
            :key="s"
            type="button"
            class="agent-starter"
            @click="useStarter(s)"
          >
            {{ s }}
          </button>
        </div>
      </div>
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
/* ── 悬浮入口：次级按钮形态 ──
   深色面 + 发丝描边 + 品牌色图标，不再用大面积实心品牌色。
   理由（DESIGN.md §5/§8）：圆角上限 6px、青色只做点缀、卡片层级靠边框不靠阴影。
   视觉重量必须低于页面主 CTA（如「新建作品」），它是辅助入口。 */
.agent-fab {
  position: fixed;
  right: var(--sp-6);
  bottom: var(--sp-6);
  z-index: 200;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  cursor: pointer;
  box-shadow: none;
  font-family: var(--font-ui);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.5;
  transition: color var(--transition-fast), border-color var(--transition-fast),
    background var(--transition-fast);
}

.agent-fab:hover {
  color: var(--text-primary);
  background: var(--bg-elevated);
  border-color: var(--brand-line);
}

.agent-fab:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.agent-fab-icon {
  flex-shrink: 0;
  color: var(--brand-primary);
}

.agent-fab-label {
  white-space: nowrap;
}

/* 快捷键提示：弱化到三级文字，仅在宽屏显示 */
.agent-fab-key {
  padding-left: var(--sp-2);
  margin-left: var(--sp-1);
  border-left: 1px solid var(--border-subtle);
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.02em;
  color: var(--text-tertiary);
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

/* ── 页头：标题在左，操作在右，会话选择独立成行 ── */
.agent-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.agent-head-title {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.agent-head-mark {
  color: var(--brand-primary);
  flex-shrink: 0;
}

.agent-head-label {
  font-family: var(--font-ui);
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
}

.agent-head-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-shrink: 0;
}

/* 纯图标按钮：28×28 触控区，与全站一致 */
.agent-icon-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 0;
  appearance: none;
  background: none;
  border: 0;
  border-radius: var(--radius);
  color: var(--text-tertiary);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast);
}

.agent-icon-btn:hover {
  color: var(--text-primary);
  background: var(--overlay-hover);
}

.agent-icon-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

/* 会话切换条：独立一行，下拉不再挤压标题与操作 */
.agent-session-bar {
  flex-shrink: 0;
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
}

.agent-session-select {
  width: 100%;
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

/* ── 空态：图标 + 一句话 + 三条引导指令 ──
   左右内边距必须与 .agent-session-bar 一致（sp-3），
   否则引导条右边缘与上方下拉、下方输入框差 4px，破坏内容网格。 */
.agent-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-8) var(--sp-3);
  text-align: center;
}

.agent-empty-mark {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  margin-bottom: var(--sp-1);
  border: 1px solid var(--brand-line);
  border-radius: var(--radius-lg);
  background: var(--brand-soft);
  color: var(--brand-primary);
}

.agent-empty-title {
  margin: 0;
  font-family: var(--font-ui);
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.agent-empty-desc {
  margin: 0;
  font-family: var(--font-ui);
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-tertiary);
}

.agent-empty-actions {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: 100%;
  margin-top: var(--sp-3);
}

/* 引导指令：可点击的整行条目，不是装饰文字 */
.agent-starter {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
  font-family: var(--font-ui);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}

.agent-starter:hover {
  color: var(--text-primary);
  border-color: var(--brand-line);
}

.agent-starter:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
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
    right: var(--sp-3);
    bottom: var(--sp-4);
  }

  /* 窄屏只留图标 + 文字，收起快捷键提示 */
  .agent-fab-key {
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
  .agent-fab,
  .agent-icon-btn,
  .agent-starter {
    transition: none;
  }
}
</style>
