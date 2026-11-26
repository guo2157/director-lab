<script setup lang="ts">
/**
 * 左栏：历史会话导航。唯一职责是「找到 / 切换 / 重命名 / 归档 / 删除」会话。
 * 三态：加载中骨架、空列表说明、加载失败可重试。
 */
import { computed, ref } from 'vue'
import { NButton, NIcon, NInput, NPopconfirm } from 'naive-ui'
import {
  Add,
  ArchiveOutline,
  ChatbubblesOutline,
  ChevronBack,
  ChevronDown,
  ChevronForward,
  Search,
  TrashOutline,
} from '@vicons/ionicons5'
import type { AgentSession } from '@/api/agent'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import { fmtTime } from './helpers'

const props = defineProps<{
  sessions: AgentSession[]
  loading: boolean
  activeId: string | null
  /** 加载失败信息（空串表示无错误） */
  error: string
  /** 已归档会话（归档可恢复；删除为物理删除） */
  archived: AgentSession[]
}>()

const emit = defineEmits<{
  select: [session: AgentSession]
  create: []
  rename: [session: AgentSession, title: string]
  archive: [session: AgentSession]
  remove: [session: AgentSession]
  restore: [session: AgentSession]
  collapse: []
  retry: []
}>()

const keyword = ref('')
const editingId = ref<string | null>(null)
const editingTitle = ref('')
const showArchived = ref(false)

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return props.sessions
  return props.sessions.filter((s) => s.title.toLowerCase().includes(kw))
})

function startRename(s: AgentSession): void {
  editingId.value = s.id
  editingTitle.value = s.title
}

function confirmRename(s: AgentSession): void {
  const title = editingTitle.value.trim()
  editingId.value = null
  if (title && title !== s.title) emit('rename', s, title)
}
</script>

<template>
  <aside class="panel sessions-panel">
    <div class="panel-header">
      <div class="panel-heading">
        <NIcon :component="ChatbubblesOutline" :size="15" class="panel-icon" />
        <span class="panel-title">会话</span>
        <span v-if="sessions.length" class="panel-count">{{ sessions.length }}</span>
      </div>
      <div class="header-actions">
        <NButton size="tiny" type="primary" @click="emit('create')">
          <template #icon><NIcon :component="Add" /></template>
          新建
        </NButton>
        <NButton
          size="tiny"
          quaternary
          class="icon-btn"
          aria-label="收起会话栏"
          title="收起会话栏"
          @click="emit('collapse')"
        >
          <template #icon><NIcon :component="ChevronBack" /></template>
        </NButton>
      </div>
    </div>

    <div class="search-box">
      <NInput
        v-model:value="keyword"
        size="small"
        placeholder="搜索会话"
        clearable
        :input-props="{ 'aria-label': '搜索会话' }"
      >
        <template #prefix><NIcon :component="Search" :size="14" /></template>
      </NInput>
    </div>

    <div class="session-scroll">
      <SkeletonList v-if="loading && !sessions.length" variant="row" :rows="6" />

      <ErrorRetry
        v-else-if="error"
        compact
        title="会话列表加载失败"
        :message="error"
        @retry="emit('retry')"
      />

      <EmptyState
        v-else-if="!filtered.length"
        compact
        :title="sessions.length ? '没有匹配的会话' : '还没有会话'"
        :description="sessions.length ? '换个关键词试试' : '新建一个会话，开始与导演台助手协作'"
      />

      <div v-else class="session-list">
        <div
          v-for="s in filtered"
          :key="s.id"
          class="session-item"
          :class="{ active: s.id === activeId, 'is-editing': editingId === s.id }"
          role="button"
          tabindex="0"
          :aria-current="s.id === activeId ? 'true' : undefined"
          @click="emit('select', s)"
          @keydown.enter.self.prevent="emit('select', s)"
          @keydown.space.self.prevent="emit('select', s)"
        >
          <template v-if="editingId === s.id">
            <NInput
              v-model:value="editingTitle"
              size="tiny"
              autofocus
              @click.stop
              @keyup.enter="confirmRename(s)"
              @blur="confirmRename(s)"
            />
          </template>
          <template v-else>
            <span class="active-bar" aria-hidden="true"></span>
            <div class="session-info">
              <div class="session-title" :title="s.title" @dblclick.stop="startRename(s)">
                {{ s.title }}
              </div>
              <div class="session-meta">
                <span class="session-time">{{ fmtTime(s.last_message_at ?? s.updated_at) }}</span>
              </div>
            </div>
            <div class="session-actions">
              <NPopconfirm @positive-click="emit('archive', s)">
                <template #trigger>
                  <NButton
                    size="tiny"
                    quaternary
                    class="row-action"
                    aria-label="归档会话"
                    title="归档会话"
                    @click.stop
                  >
                    <template #icon><NIcon :component="ArchiveOutline" /></template>
                  </NButton>
                </template>
                归档后对话内容保留，可在「已归档」中恢复。
              </NPopconfirm>
              <NPopconfirm @positive-click="emit('remove', s)">
                <template #trigger>
                  <NButton
                    size="tiny"
                    quaternary
                    type="error"
                    class="row-action"
                    aria-label="删除会话"
                    title="删除会话"
                    @click.stop
                  >
                    <template #icon><NIcon :component="TrashOutline" /></template>
                  </NButton>
                </template>
                将永久删除该会话及其全部对话上下文，不可恢复。确认删除？
              </NPopconfirm>
            </div>
          </template>
        </div>
      </div>

      <!-- 已归档：归档可恢复；删除则连同上下文物理删除 -->
      <div v-if="archived.length" class="archived-block">
        <button
          class="archived-toggle"
          type="button"
          :aria-expanded="showArchived"
          @click="showArchived = !showArchived"
        >
          <span class="archived-label">已归档 {{ archived.length }}</span>
          <NIcon
            :component="showArchived ? ChevronDown : ChevronForward"
            :size="12"
            class="archived-chev"
          />
        </button>
        <div v-if="showArchived" class="archived-list">
          <div v-for="s in archived" :key="s.id" class="archived-item">
            <span class="archived-title" :title="s.title">{{ s.title }}</span>
            <NButton size="tiny" quaternary @click="emit('restore', s)">恢复</NButton>
            <NPopconfirm @positive-click="emit('remove', s)">
              <template #trigger>
                <NButton size="tiny" quaternary type="error" @click.stop>删除</NButton>
              </template>
              将永久删除该会话及其全部对话上下文，不可恢复。确认删除？
            </NPopconfirm>
          </div>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-sheen);
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
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  gap: var(--sp-2);
  min-width: 0;
}

.panel-heading {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}

.panel-icon {
  color: var(--brand-primary);
  flex-shrink: 0;
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 会话数量：中性胶囊，不与「新建」主按钮争色 */
.panel-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--text-secondary);
  background: var(--neutral-soft);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-shrink: 0;
}

.sessions-panel {
  width: 260px;
  flex-shrink: 0;
}

.search-box {
  padding: var(--sp-2) var(--sp-2) var(--sp-1);
  flex-shrink: 0;
}

.session-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--sp-1) var(--sp-2) var(--sp-2);
}

.session-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.session-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2) var(--sp-1) var(--sp-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}

/* 选中态：品牌底色 + 左侧发光竖条（双重编码，不只靠颜色） */
.active-bar {
  position: absolute;
  left: 0;
  top: 6px;
  bottom: 6px;
  width: 2px;
  border-radius: var(--radius-pill);
  background: var(--brand-gradient-line);
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.session-item.active .active-bar {
  opacity: 1;
}

.session-item:hover {
  background: var(--overlay-hover);
}

.session-item:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 1px;
}

.session-item.active {
  background: var(--brand-soft);
  border-color: var(--brand-line);
}

.session-item.active:hover {
  background: var(--overlay-selected);
}

.session-info {
  flex: 1;
  min-width: 0;
}

.session-title {
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-item.active .session-title {
  font-weight: 600;
}

.session-meta {
  display: flex;
  gap: var(--sp-2);
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
}

.session-actions {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

/* 归档 / 删除：默认隐藏，悬停或键盘聚焦时出现，避免列表噪声 */
.row-action {
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.session-item:hover .row-action,
.session-item:focus-within .row-action,
.row-action:focus-visible {
  opacity: 1;
}

/* 已归档折叠区：常驻在列表底部，不抢占主列表视线 */
.archived-block {
  margin-top: var(--sp-2);
  padding-top: var(--sp-2);
  border-top: 1px solid var(--border-subtle);
}

.archived-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: var(--sp-1) var(--sp-2);
  background: none;
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--text-tertiary);
  font-size: 12px;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.archived-toggle:hover {
  background: var(--overlay-hover);
  color: var(--text-secondary);
}

.archived-toggle:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 1px;
}

.archived-chev {
  flex-shrink: 0;
  color: var(--text-tertiary);
}

.archived-toggle:hover .archived-chev {
  color: var(--text-secondary);
}

.archived-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: var(--sp-1);
}

.archived-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 2px var(--sp-2);
  border-radius: var(--radius);
}

.archived-item:hover {
  background: var(--overlay-hover);
}

.archived-title {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
