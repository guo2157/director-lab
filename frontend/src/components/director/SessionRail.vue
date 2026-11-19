<script setup lang="ts">
/**
 * 左栏：历史会话导航。唯一职责是「找到 / 切换 / 重命名 / 删除」会话。
 * 三态：加载中骨架、空列表说明、加载失败可重试。
 */
import { computed, ref } from 'vue'
import { NButton, NInput, NPopconfirm } from 'naive-ui'
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
  /** 已归档会话（删除即归档，可从这里恢复） */
  archived: AgentSession[]
}>()

const emit = defineEmits<{
  select: [session: AgentSession]
  create: []
  rename: [session: AgentSession, title: string]
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
      <span class="panel-title">会话</span>
      <div class="header-actions">
        <NButton size="tiny" quaternary aria-label="收起会话栏" @click="emit('collapse')">收起</NButton>
        <NButton size="tiny" type="primary" @click="emit('create')">新建</NButton>
      </div>
    </div>

    <div class="search-box">
      <NInput
        v-model:value="keyword"
        size="small"
        placeholder="搜索会话"
        clearable
        :input-props="{ 'aria-label': '搜索会话' }"
      />
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
            <div class="session-info">
              <div class="session-title" :title="s.title" @dblclick.stop="startRename(s)">
                {{ s.title }}
              </div>
              <div class="session-meta">
                <span class="session-time">{{ fmtTime(s.last_message_at ?? s.updated_at) }}</span>
              </div>
            </div>
            <NPopconfirm @positive-click="emit('remove', s)">
              <template #trigger>
                <NButton
                  size="tiny"
                  quaternary
                  type="error"
                  class="archive-btn"
                  aria-label="删除会话"
                  @click.stop
                >
                  删除
                </NButton>
              </template>
              删除后该会话将从列表移除。确认删除？
            </NPopconfirm>
          </template>
        </div>
      </div>

      <!-- 已归档：删除即归档，没有这个入口用户会以为数据被彻底删掉 -->
      <div v-if="archived.length" class="archived-block">
        <button
          class="archived-toggle"
          type="button"
          :aria-expanded="showArchived"
          @click="showArchived = !showArchived"
        >
          <span class="archived-label">已归档 {{ archived.length }}</span>
          <span class="archived-chev" aria-hidden="true">{{ showArchived ? '▾' : '▸' }}</span>
        </button>
        <div v-if="showArchived" class="archived-list">
          <div v-for="s in archived" :key="s.id" class="archived-item">
            <span class="archived-title" :title="s.title">{{ s.title }}</span>
            <NButton size="tiny" quaternary @click="emit('restore', s)">恢复</NButton>
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

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
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

.session-meta {
  display: flex;
  gap: var(--sp-2);
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
}

.archive-btn {
  opacity: 0;
  flex-shrink: 0;
}

.session-item:hover .archive-btn,
.session-item.is-editing .archive-btn {
  opacity: 1;
}

.archive-btn:focus-visible {
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
  font-size: 10px;
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
