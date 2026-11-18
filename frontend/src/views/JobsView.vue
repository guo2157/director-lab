<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, h } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpin, NEmpty, NTag, NDataTable,
  NTooltip, NStatistic, useMessage,
} from 'naive-ui'
import type { DataTableColumns } from 'naive-ui'
import { listJobs, getJobStats, retryJob, cancelJob } from '@/api/jobs'
import type { Job } from '@/types/job'

const message = useMessage()
const queryClient = useQueryClient()

// --- Filters ---
const typeFilter = ref('')
const statusFilter = ref('')
const page = ref(1)
const pageSize = 20

const typeTabs = [
  { label: '全部', value: '' },
  { label: '图像', value: 'generate_keyframe' },
  { label: '视频', value: 'generate_clip' },
  { label: '语音', value: 'generate_audio' },
  { label: 'BGM', value: 'generate_bgm' },
  { label: 'SFX', value: 'generate_sfx' },
  { label: '解析', value: 'parse' },
  { label: '合成', value: 'compose' },
  { label: '导出', value: 'export' },
]

function setStatus(value: string) {
  statusFilter.value = value
  page.value = 1
}
function setType(value: string) {
  typeFilter.value = value
  page.value = 1
}
// 任务归属描述：项目 · 第N集 · 场景 · 分镜#N（替代裸 jobId 展示）
function jobContext(job: Job): string {
  const parts: string[] = []
  if (job.project_name) parts.push(job.project_name)
  if (job.episode != null) parts.push(`第${job.episode}集`)
  if (job.scene_title) parts.push(job.scene_title)
  if (job.shot_seq != null) parts.push(`分镜#${job.shot_seq}`)
  return parts.length ? parts.join(' · ') : job.id
}

// --- Data ---
const jobsQuery = useQuery({
  queryKey: ['jobs', typeFilter, statusFilter, page],
  queryFn: () => listJobs({
    job_type: (typeFilter.value || undefined) as any,
    status: (statusFilter.value || undefined) as any,
    page: page.value,
    page_size: pageSize,
  }),
  refetchInterval: 5000,
})

const statsQuery = useQuery({
  queryKey: ['jobStats'],
  queryFn: () => getJobStats(),
  refetchInterval: 5000,
})

// --- SSE ---
const sseConnected = ref(false)
let eventSource: EventSource | null = null

onMounted(() => {
  try {
    eventSource = new EventSource('/api/events')
    eventSource.onopen = () => {
      sseConnected.value = true
    }
    eventSource.onmessage = (ev) => {
      sseConnected.value = true
      // 过滤高频流式事件：仅任务状态类事件触发列表刷新，忽略 agent.llm_delta
      try {
        const data = JSON.parse(ev.data)
        if (data.event_type === 'agent.llm_delta') return
      } catch { /* 非 JSON 消息按原逻辑刷新 */ }
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      queryClient.invalidateQueries({ queryKey: ['jobStats'] })
    }
    eventSource.onerror = () => {
      // SSE 断线降级轮询（refetchInterval 已配置）
      sseConnected.value = false
    }
  } catch {
    sseConnected.value = false
  }
})

onUnmounted(() => {
  eventSource?.close()
})

// --- Actions ---
const retryMutation = useMutation({
  mutationFn: (id: string) => retryJob(id),
  onSuccess: () => {
    message.success('重试任务已提交')
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
  },
  onError: (e: any) => message.error(`重试失败: ${e?.message ?? e}`),
})

const cancelMutation = useMutation({
  mutationFn: (id: string) => cancelJob(id),
  onSuccess: () => {
    message.success('任务已取消')
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
  },
  onError: (e: any) => message.error(`取消失败: ${e?.message ?? e}`),
})


const runningJobs = computed<Job[]>(() =>
  (jobsQuery.data.value?.items || []).filter(
    (j) => j.status === 'queued' || j.status === 'running'
  )
)

// --- Labels & colors ---
const statusColors: Record<string, string> = {
  queued: 'var(--brand-primary)', running: 'var(--brand-primary)', chained_wait: 'var(--text-secondary)',
  submission_unknown: 'var(--state-warning)', succeeded: 'var(--state-success)',
  failed: 'var(--state-danger)', lost: 'var(--state-danger)', cancelled: 'var(--text-secondary)',
}
const statusLabels: Record<string, string> = {
  queued: '排队中', running: '执行中', chained_wait: '等待前序',
  submission_unknown: '提交未知', succeeded: '成功',
  failed: '失败', lost: '丢失', cancelled: '已取消',
}
/* 与后端实际产出的 job_type 全量对齐（generate_keyframe/clip/audio/bgm/sfx、parse、compose、export、script_agent、series_agent） */
const typeLabels: Record<string, string> = {
  generate_keyframe: '图像', generate_clip: '视频', generate_audio: '语音',
  generate_bgm: 'BGM', generate_sfx: 'SFX', parse: '解析',
  compose: '合成', export: '导出',
  script_agent: '剧本分析', series_agent: '系列分析',
}

/**
 * 疑似中断：running 但已远超预期时长。
 * 依据：H3 最长片段约 25 分钟（362 帧 @24fps），90 分钟仍 running 基本是 worker 重启/进程被杀后
 * 状态未被回收的孤儿任务（本项目不在此处改后端状态机，仅做前端如实提示）。
 */
const STALL_THRESHOLD_MS = 90 * 60 * 1000
function isStalled(job: Job): boolean {
  if (job.status !== 'running') return false
  const start = new Date(job.created_at).getTime()
  return !Number.isNaN(start) && Date.now() - start > STALL_THRESHOLD_MS
}

function isEnvError(job: Job): boolean {
  // 环境类失败：连接超时、ComfyUI 不可达等；配置类：参数错误、工作流缺失等
  return job.retryable === true
}

/** 时间列：只留月-日 时:分:秒，年份对任务中心没意义 */
function fmtTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

/** 耗时：有 finished_at 才算，未结束显示运行时长 */
function fmtDuration(job: Job): string {
  const start = new Date(job.created_at).getTime()
  const end = job.finished_at ? new Date(job.finished_at).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return '—'
  const sec = Math.round((end - start) / 1000)
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  if (m < 60) return `${m}m${String(s).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

/** 状态筛选：与统计卡片共用同一个 statusFilter */
const statusTabs = [
  { label: '全部状态', value: '' },
  { label: '排队中', value: 'queued' },
  { label: '执行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
  { label: '已取消', value: 'cancelled' },
]

// --- Table ---
/** 当前页是否存在可操作任务：全成功时「操作」列整列都是 '-'，直接隐藏 */
const hasActionable = computed(() =>
  (jobsQuery.data.value?.items ?? []).some((j) =>
    ['failed', 'lost', 'queued', 'running', 'chained_wait', 'submission_unknown'].includes(j.status),
  ),
)

const columns = computed<DataTableColumns<Job>>(() => {
  const cols: DataTableColumns<Job> = [
  { title: '归属', key: 'project_name', minWidth: 240, ellipsis: { tooltip: true },
    render: (row) => jobContext(row) },
  { title: '类型', key: 'job_type', width: 76,
    render: (row) => typeLabels[row.job_type] || row.job_type },
  // 状态列兼带重试次数与进度：进度条只对执行中有意义
  { title: '状态', key: 'status', width: 132,
    render: (row) => {
      const tag = h(NTag, {
        size: 'small',
        round: true,
        color: { color: statusColors[row.status], textColor: 'var(--text-primary)' },
      }, { default: () => statusLabels[row.status] || row.status })
      const extra: Array<ReturnType<typeof h>> = []
      if ((row.attempts ?? 1) > 1) {
        extra.push(h('span', { class: 'cell-note' }, `第 ${row.attempts} 次`))
      }
      if (row.status === 'running' && row.progress != null) {
        extra.push(h('span', { class: 'cell-note' }, `${row.progress}%`))
      }
      if (isStalled(row)) {
        extra.push(h('span', { class: 'cell-note is-stall' }, '可能已中断'))
      }
      if (!extra.length) return tag
      return h('span', { class: 'cell-status' }, [tag, ...extra])
    } },
  { title: '耗时', key: 'duration', width: 82,
    render: (row) => fmtDuration(row) },
  { title: '创建时间', key: 'created_at', width: 116,
    render: (row) => fmtTime(row.created_at) },
  ]
  if (!hasActionable.value) return cols
  cols.push({ title: '操作', key: 'actions', width: 92,
    render: (row) => {
      if (row.status === 'failed' || row.status === 'lost') {
        if (isEnvError(row)) {
          return h(NButton, { size: 'tiny', type: 'primary', quaternary: true,
            onClick: () => retryMutation.mutate(row.id) }, { default: () => '重试' })
        }
        return h(NTooltip, { style: { maxWidth: '460px' } }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true }, { default: () => '查看原因' }),
          default: () => h('div', { class: 'err-tip' }, row.error_msg || '未记录错误原因'),
        })
      }
      if (row.status === 'queued' || row.status === 'running' || row.status === 'chained_wait') {
        return h(NButton, { size: 'tiny', type: 'error', quaternary: true,
          onClick: () => cancelMutation.mutate(row.id) }, { default: () => '取消' })
      }
      return '-'
    },
  })
  return cols
})

</script>

<template>
  <div class="jobs-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">任务中心</h2>
      </div>
      <div class="sse-indicator" :class="sseConnected ? 'on' : 'off'">
        <span class="sse-dot"></span>
        {{ sseConnected ? '实时连接' : '轮询模式' }}
      </div>
    </div>

    <!-- Stats cards（点击联动过滤） -->
    <div class="stats-row">
      <n-card class="stat-card clickable" :bordered="false" @click="setStatus('succeeded')">
        <n-statistic label="今日成功" :value="statsQuery.data.value?.today_succeeded ?? 0" />
      </n-card>
      <n-card class="stat-card clickable" :bordered="false" @click="setStatus('failed')">
        <n-statistic label="今日失败" :value="statsQuery.data.value?.today_failed ?? 0" />
      </n-card>
      <n-card class="stat-card clickable" :bordered="false" @click="setStatus('running')">
        <n-statistic label="进行中" :value="statsQuery.data.value?.in_progress ?? 0" />
      </n-card>
      <n-card class="stat-card" :bordered="false">
        <n-statistic label="平均耗时(秒)" :value="Math.round(statsQuery.data.value?.avg_duration_seconds ?? 0)" />
      </n-card>
    </div>

    <!-- 主从布局：左侧任务列表 + 右侧详情面板 -->
    <div class="jobs-layout">
      <div class="jobs-main">
        <n-card class="panel-card" :bordered="false">
          <div class="wf-toolbar">
            <div class="filter-row">
              <div class="type-tabs">
                <button
                  v-for="t in typeTabs"
                  :key="t.value"
                  type="button"
                  class="status-tab"
                  :class="{ active: typeFilter === t.value }"
                  @click="setType(t.value)"
                >{{ t.label }}</button>
              </div>
              <n-button size="small" :loading="jobsQuery.isFetching.value" @click="() => jobsQuery.refetch()">刷新</n-button>
            </div>
            <!-- 状态筛选：类型与状态两条独立轴，避免只能靠统计卡片点进去 -->
            <div class="filter-row is-status">
              <div class="type-tabs">
                <button
                  v-for="s in statusTabs"
                  :key="s.value"
                  type="button"
                  class="status-tab"
                  :class="{ active: statusFilter === s.value }"
                  @click="setStatus(s.value)"
                >{{ s.label }}</button>
              </div>
              <span class="result-hint">
                共 {{ jobsQuery.data.value?.total ?? 0 }} 个任务
                · 每页 {{ pageSize }} 条
              </span>
            </div>
          </div>

          <!-- 进行中任务置顶卡片 -->
          <div v-if="runningJobs.length" class="running-cards">
            <div
              v-for="job in runningJobs"
              :key="job.id"
              class="running-card"
            >
              <span class="running-pulse"></span>
              <span class="running-type">{{ typeLabels[job.job_type] || job.job_type }}</span>
              <span class="running-id">{{ jobContext(job) }}</span>
              <n-button size="tiny" type="error" quaternary @click.stop="cancelMutation.mutate(job.id)">取消</n-button>
            </div>
          </div>

          <n-spin v-if="jobsQuery.isLoading.value" class="page-loading" />
          <div v-else-if="jobsQuery.isError.value" class="state-block">
            <p class="state-text">任务加载失败：{{ jobsQuery.error.value instanceof Error ? jobsQuery.error.value.message : '请稍后重试' }}</p>
            <n-button secondary @click="() => jobsQuery.refetch()">重试</n-button>
          </div>
          <n-empty v-else-if="jobsQuery.data.value?.items?.length === 0" description="暂无任务" style="padding: 24px 0" />
          <n-data-table
            v-else
            :columns="columns"
            :data="jobsQuery.data.value?.items || []"
            :row-key="(row: Job) => row.id"
            :bordered="false"
            :single-line="false"
            size="small"
            :max-height="'calc(100vh - 430px)'"
            :pagination="{
              page: page,
              pageSize: pageSize,
              itemCount: jobsQuery.data.value?.total ?? 0,
              onUpdatePage: (p: number) => { page = p },
              showSizePicker: false,
            }"
          />
        </n-card>
      </div>

    </div>
  </div>
</template>

<style scoped>
.jobs-page {
  padding: var(--sp-6);
  max-width: 1440px;
  margin: 0 auto;
  min-height: calc(100vh - var(--app-header-height, 56px));
}
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  margin: var(--sp-1) 0 var(--sp-4);
}
.sse-indicator {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--text-secondary);
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--radius-pill);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.sse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.sse-indicator.on .sse-dot {
  background: var(--state-success);
  box-shadow: 0 0 0 3px var(--success-line);
}
.sse-indicator.off .sse-dot {
  background: var(--state-warning);
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.stat-card {
  border-radius: var(--radius-lg);
  transition: border-color var(--transition-fast);
}
.stat-card.clickable {
  cursor: pointer;
}
/* 深色界面用边框明度变化表达可点击，不做位移与投影 */
.stat-card.clickable:hover {
  border-color: var(--brand-primary);
}
.jobs-layout {
  display: block;
}
.jobs-main {
  min-width: 0;
}
.panel-card {
  border-radius: var(--radius-lg);
}
.wf-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
  flex-wrap: wrap;
}
.status-tab {
  appearance: none;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  padding: 5px 14px;
  border-radius: var(--radius-pill);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.status-tab:hover {
  color: var(--text-primary);
}
.status-tab.active {
  background: var(--brand-primary);
  color: var(--text-primary);
}
.filter-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.type-tabs {
  display: flex;
  gap: var(--sp-1);
  padding: 3px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  flex-wrap: wrap;
}
.running-cards {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.running-card {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}
.running-card:hover {
  border-color: var(--border-strong);
}
.running-card.selected {
  border-color: var(--brand-primary);
  background: var(--brand-soft);
}
.running-pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--brand-primary);
  animation: running-pulse 1.6s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes running-pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 var(--brand-line); }
  50% { opacity: 0.6; box-shadow: 0 0 0 5px var(--brand-soft); }
}
.running-type {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  flex-shrink: 0;
}
.running-id {
  font-size: 12px;
  color: var(--text-secondary);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-12) var(--sp-6);
}
.state-text {
  margin: 0;
  color: var(--text-secondary);
  font-size: 14px;
}
.page-loading {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}
@media (max-width: 768px) {
  .stats-row {
    grid-template-columns: repeat(2, 1fr);
  }
  .wf-toolbar {
    flex-direction: column;
    align-items: stretch;
  }
}
.filter-row.is-status {
  margin-top: var(--sp-2);
}
.result-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
/* 状态列内的次要信息（重试次数 / 进度） */
:deep(.cell-status) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
:deep(.cell-note.is-stall) {
  color: var(--state-warning);
}
:deep(.cell-note) {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
/* 表格：行高收紧一档，长列表一屏多看几条 */
:deep(.n-data-table-td) {
  padding-top: 7px;
  padding-bottom: 7px;
}
:deep(.n-data-table-th) {
  font-size: 12px;
  color: var(--text-tertiary);
}

/* 失败原因：允许长错误换行，避免 tooltip 单行溢出不可读 */
.err-tip {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  max-height: 280px;
  overflow-y: auto;
}
</style>