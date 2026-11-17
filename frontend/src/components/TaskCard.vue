<script setup lang="ts">
import { computed } from 'vue'
import { NIcon, NTag } from 'naive-ui'
import { CheckmarkCircle, CloseCircle, DownloadOutline, FilmOutline, TimeOutline } from '@vicons/ionicons5'
import type { TaskCardData } from '@/api/tasks'

const props = defineProps<{
  /** 初始卡片（assistant payload.task_card） */
  card: TaskCardData
  /** SSE 事件合并后的最新快照（无则回退初始卡片） */
  snapshot?: (Partial<TaskCardData> & { lastEvent?: unknown }) | null
}>()

/** 合并视图：初始卡片 + SSE 快照（快照优先，单卡片动态更新不刷屏） */
const view = computed<TaskCardData>(() => ({
  ...props.card,
  ...(props.snapshot ?? {}),
}))

const STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  running: '进行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
const STATUS_TYPE: Record<string, 'default' | 'info' | 'success' | 'error' | 'warning'> = {
  pending: 'default',
  running: 'info',
  completed: 'success',
  failed: 'error',
  cancelled: 'warning',
}
const TASK_TYPE_TEXT: Record<string, string> = {
  parse: '剧本解析',
  generate: '生成',
  compose: '视频合成',
  export: '导出',
}

const statusText = computed(() => STATUS_TEXT[view.value.status] ?? view.value.status)
const statusType = computed(() => STATUS_TYPE[view.value.status] ?? 'default')
const taskTypeText = computed(
  () => TASK_TYPE_TEXT[view.value.task_type] ?? view.value.task_type,
)
const progressPercent = computed(() =>
  Math.max(0, Math.min(100, Math.round(view.value.progress ?? 0))),
)
const isTerminal = computed(() => ['completed', 'failed', 'cancelled'].includes(view.value.status))

/** 结果卡片常用字段（composition_id/export_id/download_url 等） */
const resultEntries = computed(() => {
  const r = view.value.result
  if (!r || typeof r !== 'object') return []
  return Object.entries(r).filter(
    ([, v]) => v != null && (typeof v !== 'object' || Array.isArray(v)),
  )
})
const downloadUrl = computed(() => {
  const r = view.value.result as Record<string, unknown> | null
  const u = r?.download_url
  return typeof u === 'string' ? u : null
})
</script>

<template>
  <div class="task-card" :data-status="view.status">
    <div class="task-card-header">
      <span class="task-card-icon"><n-icon :component="FilmOutline" :size="16" /></span>
      <span class="task-card-title" :title="view.title">{{ view.title }}</span>
      <NTag size="small" :type="statusType" :bordered="false">{{ statusText }}</NTag>
    </div>
    <div class="task-card-meta">
      <span>{{ taskTypeText }}</span>
      <span v-if="!isTerminal">进度 {{ progressPercent }}%</span>
      <span v-if="view.error_code" class="task-card-error-code">{{ view.error_code }}</span>
    </div>
    <!-- 进度条：非终态展示，单卡片内动态更新 -->
    <div v-if="!isTerminal" class="task-card-progress">
      <div class="task-card-progress-bar" :style="{ width: `${progressPercent}%` }" />
    </div>
    <!-- 阶段列表 -->
    <div v-if="view.stages?.length" class="task-card-stages">
      <div
        v-for="s in view.stages"
        :key="s.stage_id"
        class="task-stage"
        :data-status="s.status"
      >
        <span class="task-stage-dot" />
        <span class="task-stage-name">{{ s.stage_name }}</span>
        <span class="task-stage-status">
          <template v-if="s.status === 'running'">{{ Math.round(s.progress ?? 0) }}%</template>
          <n-icon
            v-else
            :component="s.status === 'succeeded' ? CheckmarkCircle : s.status === 'failed' ? CloseCircle : TimeOutline"
            :size="14"
          />
        </span>
      </div>
    </div>
    <!-- 错误信息 -->
    <div v-if="view.error_msg" class="task-card-error">{{ view.error_msg }}</div>
    <!-- 结果摘要 -->
    <div v-if="isTerminal && view.status === 'completed' && resultEntries.length" class="task-card-result">
      <div v-for="[k, v] in resultEntries.slice(0, 6)" :key="k" class="task-card-result-item">
        <span class="task-card-result-key">{{ k }}</span>
        <span class="task-card-result-value">{{ Array.isArray(v) ? v.length : String(v).slice(0, 80) }}</span>
      </div>
    </div>
    <a
n      v-if="downloadUrl"
      class="task-card-download"
      :href="downloadUrl"
      target="_blank"
      rel="noopener"
    ><n-icon :component="DownloadOutline" :size="14" /> 下载结果</a>
  </div>
</template>

<style scoped>
.task-card {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  background: var(--n-color);
  font-size: 13px;
}
.task-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.task-card-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}
.task-card-meta {
  display: flex;
  gap: 12px;
  margin-top: 4px;
  color: var(--text-tertiary);
  font-size: 12px;
}
.task-card-error-code {
  color: var(--state-danger);
}
.task-card-progress {
  margin-top: 8px;
  height: 4px;
  border-radius: 2px;
  background: var(--border-subtle);
  overflow: hidden;
}
.task-card-progress-bar {
  height: 100%;
  border-radius: 2px;
  background: var(--brand-primary);
  transition: width 0.3s ease;
}
.task-card-stages {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.task-stage {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.task-stage[data-status='succeeded'] {
  color: var(--state-success);
}
.task-stage[data-status='failed'] {
  color: var(--state-danger);
}
.task-stage[data-status='running'] {
  color: var(--brand-primary);
}
.task-stage-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.task-stage-name {
  flex: 1;
}
.task-stage-status {
  font-variant-numeric: tabular-nums;
}
.task-card-error {
  margin-top: 8px;
  padding: 6px 8px;
  border-radius: 4px;
  background: var(--danger-soft);
  color: var(--state-danger);
  font-size: 12px;
  word-break: break-all;
}
.task-card-result {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.task-card-result-item {
  display: flex;
  gap: 8px;
  font-size: 12px;
}
.task-card-result-key {
  color: var(--text-tertiary);
  min-width: 96px;
}
.task-card-result-value {
  color: var(--text-primary);
  word-break: break-all;
}
.task-card-download {
  display: inline-block;
  margin-top: 8px;
  color: var(--brand-primary);
  font-size: 12px;
  text-decoration: none;
}
.task-card-download:hover {
  text-decoration: underline;
}
</style>
