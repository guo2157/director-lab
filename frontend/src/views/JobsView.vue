<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, h } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpin, NEmpty, NDataTable,
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

/**
 * 窄屏切卡片视图。
 * 为什么用 JS 而不是纯 CSS：表格是 n-data-table，列宽由内部 colgroup 决定，
 * 纯 CSS 无法把 <tr> 变成卡片（列宽、边框、colspan 都在组件内部）。
 * 断点用 768（与 check:breakpoints 白名单一致）。
 */
const isNarrow = ref(typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches)
const onResize = () => {
  isNarrow.value = window.matchMedia('(max-width: 768px)').matches
}
/** 窄屏展开中的错误详情行（无 hover，tooltip 不可达） */
const showErrorJob = ref<Job | null>(null)

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
  window.addEventListener('resize', onResize)
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
  window.removeEventListener('resize', onResize)
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

// --- Labels ---
/* 状态颜色由 .job-status.is-* 类决定（见 <style>），此处不再维护色值表 */
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

/**
 * 注意：不要用后端 retryable 判断错误性质 —— 它只表示「还能再试」
 * （failed/lost && attempts < max_attempts），与环境错误无关。
 * 实测 comfy_unreachable(23) 和 llm_invalid_output(12) 的 retryable 都是 true，
 * 所以失败任务一律同时给「重试」+「查看原因」，不做错误分类臆断。
 */

/**
 * error_code → 人话。后端 error_msg 常以英文技术细节为主
 * （实测 comfy_unreachable 带完整实例 URL、llm_invalid_output 带 JSON 解析位置），
 * 普通用户看不出「该修什么」。这里只解释「是什么问题、该做什么」，
 * 原文仍然完整保留在 error_msg 里，供排查使用。
 */
const errorHints: Record<string, string> = {
  comfy_unreachable: 'ComfyUI 实例连不上，检查实例地址或重新启用',
  comfy_execution_error: 'ComfyUI 工作流执行报错，检查节点参数',
  llm_invalid_output: '模型返回内容无法解析，可重试或降低输出长度',
  llm_output_truncated: '模型输出被截断，可提高 max_tokens 或缩短输入',
}

function errorHint(code?: string | null): string | null {
  return code ? (errorHints[code] ?? null) : null
}

/**
 * 失败原因摘要：卡片默认只显示一行，展开后看全文。
 * 后端的 error_msg 常带很长的 URL / JSON 片段（实测 comfy_unreachable 里有完整实例地址），
 * 不截断会把卡片撑高。
 */
function truncateError(msg?: string | null): string {
  if (!msg) return '未记录错误原因'
  const one = msg.replace(/\s+/g, ' ').trim()
  return one.length > 60 ? `${one.slice(0, 60)}…` : one
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
  return fmtSeconds(Math.round((end - start) / 1000))
}

/** 秒 → 人类可读时长。统计卡与表格共用同一函数，避免「93 秒」与「2m57s」两种写法并存。 */
function fmtSeconds(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '—'
  if (sec < 60) return `${sec}s`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  if (m < 60) return `${m}m${String(s).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

/** 统计卡的平均耗时：与表格耗时列同格式 */
const avgDurationText = computed(() =>
  fmtSeconds(Math.round(statsQuery.data.value?.avg_duration_seconds ?? 0)),
)

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
  { title: '归属', key: 'project_name', minWidth: 200, ellipsis: { tooltip: true },
    render: (row) => jobContext(row) },
  { title: '类型', key: 'job_type', width: 76,
    render: (row) => typeLabels[row.job_type] || row.job_type },
  // 状态列兼带重试次数与进度：进度条只对执行中有意义
  { title: '状态', key: 'status', width: 148,
    render: (row) => {
      // 状态标记：圆点 + 文字双通道。颜色一律由 CSS 类决定（内联 style 会压过类规则，
      // 导致「成功态降权」失效），这里只挂语义类名。
      const tag = h('span', {
        class: ['job-status', `is-${row.status}`],
      }, [
        h('span', { class: 'job-status-dot' }),
        statusLabels[row.status] || row.status,
      ])
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
  { title: '耗时', key: 'duration', width: 84, align: 'right',
    className: 'col-num',
    render: (row) => fmtDuration(row) },
  { title: '创建时间', key: 'created_at', width: 124, align: 'right',
    className: 'col-num',
    render: (row) => fmtTime(row.created_at) },
  ]
  if (!hasActionable.value) return cols
  cols.push({ title: '操作', key: 'actions', width: 132,
    render: (row) => {
      if (row.status === 'failed' || row.status === 'lost') {
        /**
         * 失败任务同时给「重试」和「查看原因」。
         * 原实现用 isEnvError 二选一，但 isEnvError 的实现是 `retryable === true`，
         * 而后端 retryable 的定义只是 `failed/lost && attempts < job_max_attempts`
         * （api/v1/jobs.py:29）—— 跟「是不是环境错误」无关。
         * 结果：llm_invalid_output（DB 实测 12 条）这类配置错误也被判成
         * 「环境错误」，只显示「重试」，用户永远看不到失败原因。
         * 这里不臆断错误分类，两个动作都给，与窄屏卡片视图一致。
         */
        const btns = []
        if (row.retryable) {
          btns.push(h(NButton, { size: 'tiny', type: 'primary', quaternary: true,
            onClick: () => retryMutation.mutate(row.id) }, { default: () => '重试' }))
        }
        btns.push(h(NTooltip, { style: { maxWidth: '460px' } }, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true }, { default: () => '查看原因' }),
          default: () => h('div', { class: 'err-tip' }, row.error_msg || '未记录错误原因'),
        }))
        return h('span', { class: 'cell-actions' }, btns)
      }
      if (row.status === 'queued' || row.status === 'running' || row.status === 'chained_wait') {
        return h(NButton, { size: 'tiny', type: 'error', quaternary: true,
          onClick: () => cancelMutation.mutate(row.id) }, { default: () => '取消' })
      }
      // 无可用操作：留空白而不是 '-'，避免把空值读成数据
      return h('span', { class: 'cell-empty', 'aria-hidden': 'true' })
    },
  })
  return cols
})

</script>

<template>
  <div class="jobs-page">
    <header class="jobs-head">
      <div class="head-copy">
        <h1 class="jobs-title">任务中心</h1>
        <p class="jobs-sub">
          共 {{ jobsQuery.data.value?.total ?? 0 }} 个任务 · 每页 {{ pageSize }} 条
        </p>
      </div>
      <div class="head-actions">
        <span class="sse-indicator" :class="sseConnected ? 'on' : 'off'">
          <span class="sse-dot" aria-hidden="true"></span>
          {{ sseConnected ? '实时连接' : '轮询模式' }}
        </span>
        <n-button size="small" :loading="jobsQuery.isFetching.value" @click="() => jobsQuery.refetch()">
          刷新
        </n-button>
      </div>
    </header>

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
        <!-- 用同一个 n-statistic 组件承载时长（value 支持字符串），
             自绘 div 会因 Naive 内部 .n-statistic-value 的行高盒模型差 2px，四卡基线对不齐 -->
        <n-statistic label="平均耗时" :value="avgDurationText" />
      </n-card>
    </div>

    <!-- 主从布局：左侧任务列表 + 右侧详情面板 -->
    <div class="jobs-layout">
      <div class="jobs-main">
        <n-card class="panel-card" :bordered="false">
          <div class="wf-toolbar">
            <div class="filter-row">
              <span class="filter-label" aria-hidden="true">类型</span>
              <div class="type-tabs">
                <button
                  v-for="t in typeTabs"
                  :key="t.value"
                  type="button"
                  class="status-tab is-type"
                  :class="{ active: typeFilter === t.value }"
                  @click="setType(t.value)"
                >{{ t.label }}</button>
              </div>
            </div>
            <!-- 状态筛选：类型与状态两条独立轴，避免只能靠统计卡片点进去 -->
            <div class="filter-row is-status">
              <span class="filter-label" aria-hidden="true">状态</span>
              <div class="type-tabs">
                <button
                  v-for="s in statusTabs"
                  :key="s.value"
                  type="button"
                  class="status-tab is-status"
                  :class="{ active: statusFilter === s.value }"
                  @click="setStatus(s.value)"
                >{{ s.label }}</button>
              </div>
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

          <!-- 移动端：表格 6 列合计 724px，390px 视口下只能横向滚动，
               而移动端滚动条默认隐藏、无任何提示，右侧「重试/取消」等于不存在。
               改为卡片：一行主信息 + 一行状态元信息 + 操作，全部纵向可见。 -->
          <div v-else-if="isNarrow" class="job-cards">
            <article v-for="job in jobsQuery.data.value?.items || []" :key="job.id" class="job-card">
              <div class="job-card-top">
                <span class="job-card-type">{{ typeLabels[job.job_type] || job.job_type }}</span>
                <span class="job-status" :class="`is-${job.status}`">
                  <span class="job-status-dot" />
                  {{ statusLabels[job.status] || job.status }}
                </span>
              </div>
              <p class="job-card-context">{{ jobContext(job) }}</p>
              <div class="job-card-meta">
                <span>{{ fmtDuration(job) }}</span>
                <span>{{ fmtTime(job.created_at) }}</span>
                <span v-if="(job.attempts ?? 1) > 1">第 {{ job.attempts }} 次</span>
                <span v-if="job.status === 'running' && job.progress != null">{{ job.progress }}%</span>
                <span v-if="isStalled(job)" class="is-stall">可能已中断</span>
              </div>
              <div v-if="job.status === 'failed' || job.status === 'lost' || job.status === 'queued' || job.status === 'running' || job.status === 'chained_wait'" class="job-card-actions">
                <n-button
                  v-if="job.status === 'failed' || job.status === 'lost'"
                  size="tiny"
                  type="primary"
                  quaternary
                  @click="retryMutation.mutate(job.id)"
                >重试</n-button>
                <n-button
                  v-else
                  size="tiny"
                  type="error"
                  quaternary
                  @click="cancelMutation.mutate(job.id)"
                >取消</n-button>
                <n-button
                  v-if="job.status === 'failed' || job.status === 'lost'"
                  size="tiny"
                  quaternary
                  @click="showErrorJob = showErrorJob?.id === job.id ? null : job"
                >{{ showErrorJob?.id === job.id ? '收起原因' : '查看原因' }}</n-button>
              </div>
              <!-- 失败原因常驻展示首行：窄屏没有 hover，若只在「查看原因」里，
                   用户看到「失败 + 重试」但永远不知道错在哪。
                   先给中文解释（error_code 映射），再给后端原文。 -->
              <div v-if="(job.status === 'failed' || job.status === 'lost') && (job.error_msg || job.error_code)" class="job-card-error is-inline">
                <span v-if="errorHint(job.error_code)" class="err-hint">{{ errorHint(job.error_code) }}</span>
                <span class="err-raw">{{ showErrorJob?.id === job.id ? (job.error_msg || '未记录错误原因') : truncateError(job.error_msg) }}</span>
              </div>
            </article>
          </div>

          <n-data-table
            v-else
            :columns="columns"
            :data="jobsQuery.data.value?.items || []"
            :row-key="(row: Job) => row.id"
            :bordered="false"
            :single-line="false"
            size="small"
            :max-height="'calc(100vh - 470px)'"
            :pagination="{
              page: page,
              pageSize: pageSize,
              itemCount: jobsQuery.data.value?.total ?? 0,
              onUpdatePage: (p: number) => { page = p },
              showSizePicker: false,
            }"
          />
          <!-- 卡片视图没有表格内建分页，单独给一个 -->
          <div v-if="isNarrow && (jobsQuery.data.value?.total ?? 0) > pageSize" class="card-pager">
            <n-button size="small" :disabled="page <= 1" @click="page--">上一页</n-button>
            <span class="card-pager-text">{{ page }} / {{ Math.ceil((jobsQuery.data.value?.total ?? 0) / pageSize) }}</span>
            <n-button size="small" :disabled="page >= Math.ceil((jobsQuery.data.value?.total ?? 0) / pageSize)" @click="page++">下一页</n-button>
          </div>
        </n-card>
      </div>

    </div>
  </div>
</template>

<style scoped>
.jobs-page {
  padding: var(--sp-6) var(--sp-6) var(--sp-16);
  max-width: 1440px;
  margin: 0 auto;
  min-height: calc(100vh - var(--app-header-height, 64px));
}

/* ---------- 页头：与作品列表同构（大标题 + 副标题 + 右侧状态/操作） ---------- */
.jobs-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--sp-4);
  margin: 2px 0 var(--sp-6);
}
.head-copy {
  min-width: 0;
}
.jobs-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 34px;
  font-weight: 700;
  letter-spacing: -0.028em;
  line-height: 1.15;
  color: var(--text-primary);
}
.jobs-sub {
  margin: 7px 0 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}

.sse-indicator {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--text-secondary);
  padding: var(--sp-1) var(--sp-3);
  border-radius: var(--radius-pill);
  background: var(--bg-panel);
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
/* 统计卡：用边框而非投影分层；可点卡片给出明确的悬停与指针语义 */
.stat-card {
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  transition:
    border-color var(--transition-fast),
    background-color var(--transition-fast);
}
.stat-card.clickable {
  cursor: pointer;
}
.stat-card.clickable:hover {
  border-color: var(--brand-line);
  background: var(--bg-elevated);
}
.stat-card.clickable:hover :deep(.n-statistic-value__content) {
  color: var(--brand-primary);
}
.stat-card.clickable:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
/* 统计值统一等宽数字，四张卡的数字基线一致 */
.stat-card :deep(.n-statistic-value__content) {
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
  font-size: 24px;
  line-height: 1.2;
}
/* Naive 的 label 类名是 .n-statistic__label（双下划线）。
   写成 .n-statistic-label 会静默失效，四卡 label 行高不同 → 数值基线错开。 */
.stat-card :deep(.n-statistic__label) {
  font-size: 12.5px;
  line-height: 22px;
  color: var(--text-tertiary);
  margin-bottom: 2px;
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
/* 激活态：品牌底 + 品牌字（原实心品牌底 + 浅字对比不足 3:1），并加粗做第二编码 */
.status-tab.active {
  background: var(--brand-soft);
  color: var(--brand-primary);
  font-weight: 500;
  box-shadow: inset 0 0 0 1px var(--brand-line);
}
/* 状态轴与类型轴分级：类型是主筛选（品牌 soft + 品牌字），状态是次筛选（中性 soft + 正文色）。
   两条轴都用品牌色会出现两个「当前选中」，用户分不清哪个是主筛选。 */
.type-tabs .status-tab.is-status.active {
  background: var(--neutral-soft);
  color: var(--text-primary);
  box-shadow: inset 0 0 0 1px var(--border-strong);
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
    gap: var(--sp-2);
    margin-bottom: var(--sp-3);
  }
  /* 统计卡在窄屏压缩：4 张卡 206px 吃掉首屏 1/4，
     而它们是「概览」不是「主操作」，不值得占这么多高度 */
  .stat-card {
    border-radius: var(--radius-md);
  }
  /* Naive 的 card 内容容器类名是 .n-card-content（单横线），
     不是 .n-card__content —— 后者是 Vue 3 常见误写，写了也不报错、只是不生效。
     实测 390px 下默认 padding 20px 24px 让卡片多出 42px 高度。 */
  .stat-card :deep(.n-card-content) {
    padding: 10px 12px;
  }
  .stat-card :deep(.n-statistic-value__content) {
    font-size: 20px;
  }
  /* 类名必须是 .n-statistic__label（双下划线）。写成 .n-statistic-label 会静默失效，
     文件里 557 行已有同样的教训注释。 */
  .stat-card :deep(.n-statistic__label) {
    font-size: 11.5px;
    line-height: 18px;
  }
  .wf-toolbar {
    flex-direction: column;
    align-items: stretch;
    gap: var(--sp-2);
  }
  /* 筛选轴改横向滚动单行。
     15 个按钮 wrap 成 5 行要 240px（首屏 28%），而筛选是低频操作；
     横向滑动是移动端 tab 的通行交互，比纵向堆 5 行更省空间也更好扫。
     关键：type-tabs 必须 flex: 1 1 0%（basis 0 而非 auto）。
     用 auto 时基准宽 = 内容宽 527px，它拒绝收缩，整条链一路撑到 565px，
     实测把 .wf-toolbar(252px) 和 .n-card-content(300px) 全部顶破。
     basis 0 才能让它在剩余空间里定位、由 overflow-x 自己滚。 */
  .filter-row {
    flex-wrap: nowrap;
    width: 100%;
    max-width: 100%;
    min-width: 0;
  }
  .filter-row .type-tabs {
    flex: 1 1 0%;
    min-width: 0;
    flex-wrap: nowrap;
    overflow-x: auto;
    overflow-y: hidden;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .type-tabs::-webkit-scrollbar {
    display: none;
  }
  .status-tab {
    flex: none;
    white-space: nowrap;
  }
}

/* --- 窄屏任务卡片（替代表格，见 template 内注释） --- */
.job-cards {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  /* 底部必须避开右下角固定的 AI 助手 FAB（bottom 24 + 高 38 = 62px 占位）。
     与 ScriptReader.vue:87 同一处理：FAB 是 position:fixed，会压住最后一张卡的
     操作按钮和错误原文，而失败原因恰恰在卡片最底部。 */
  padding-bottom: 62px;
}
.job-card {
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
}
.job-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.job-card-type {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
/* 归属信息可能很长（项目 · 第N集 · 场景 · 分镜#N），允许两行后省略，
   不强制 nowrap：窄屏下强撑单行会把整卡顶宽 */
.job-card-context {
  margin: 6px 0 0;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--text-secondary);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.job-card-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.job-card-meta .is-stall {
  color: var(--warning-text, var(--text-secondary));
}
.job-card-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: 10px;
}
/* 错误原文：等宽不必要，但必须能换行完整阅读（这是用户点「查看原因」的唯一去处） */
.job-card-error {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  font-size: 12px;
  line-height: 1.55;
  color: var(--danger-text);
  word-break: break-word;
}
/* 常驻摘要态：视觉上比展开态轻，避免每张失败卡都像报警框 */
.job-card-error.is-inline {
  margin-top: 8px;
  padding: 6px 9px;
  background: transparent;
  border-left: 2px solid var(--danger);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  color: var(--text-secondary);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
/* 中文解释在前、后端原文在后：先让人看懂，再给排查线索 */
.job-card-error .err-hint {
  color: var(--danger-text);
  font-weight: 500;
}
.job-card-error .err-raw {
  color: var(--text-tertiary);
  font-size: 11.5px;
  word-break: break-word;
}
.card-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  padding: var(--sp-3) 0 0;
}
.card-pager-text {
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.filter-row.is-status {
  margin-top: var(--sp-2);
}
/* 两条筛选轴的显式标签：不靠位置区分「类型」与「状态」，深色底上更需要文字锚点 */
.filter-label {
  flex: none;
  min-width: 28px;
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
  letter-spacing: 0.02em;
}
.result-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
/* 状态列内的次要信息（重试次数 / 进度） */
:deep(.cell-status) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
/* 失败任务的操作列同时放「重试」+「查看原因」，需要横排且不换行 */
:deep(.cell-actions) {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  white-space: nowrap;
}
/* 状态标记：圆点 + 文字，颜色只作辅助通道，避免整块填充抢走表格阅读节奏。
   颜色必须由类驱动（内联 style 优先级更高会让下面的降权规则全部失效）。 */
:deep(.job-status) {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12.5px;
  line-height: 18px;
  font-weight: 500;
  white-space: nowrap;
}
:deep(.job-status-dot) {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}
/* 逐状态配色：文字色 + 圆点色分开控制，成功态走中性灰（默认结果不该刷存在感） */
:deep(.job-status.is-succeeded) {
  color: var(--text-secondary);
}
:deep(.job-status.is-succeeded .job-status-dot) {
  background: var(--state-success);
}
:deep(.job-status.is-failed),
:deep(.job-status.is-lost) {
  color: var(--danger-text);
}
:deep(.job-status.is-failed .job-status-dot),
:deep(.job-status.is-lost .job-status-dot) {
  background: var(--danger);
}
:deep(.job-status.is-queued),
:deep(.job-status.is-running) {
  color: var(--brand-primary);
}
:deep(.job-status.is-submission_unknown) {
  color: var(--state-warning);
}
:deep(.job-status.is-cancelled),
:deep(.job-status.is-chained_wait) {
  color: var(--text-tertiary);
}
/* 执行中：脉冲，与「排队中」区分 */
:deep(.job-status.is-running .job-status-dot) {
  animation: job-dot-pulse 1.6s ease-in-out infinite;
}
@keyframes job-dot-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
:deep(.cell-note.is-stall) {
  color: var(--state-warning);
}
:deep(.cell-note) {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
/* 空操作位：留白，不显示 '-' */
:deep(.cell-empty) {
  display: inline-block;
  min-width: 1px;
}
/* 数字列：右对齐 + 等宽数字，方便纵向比较耗时与时间 */
:deep(.col-num) {
  font-variant-numeric: tabular-nums;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}
/* 表格：行高收紧一档，长列表一屏多看几条 */
:deep(.n-data-table-td) {
  padding-top: 6px;
  padding-bottom: 6px;
}
:deep(.n-data-table-th) {
  font-size: 12px;
  color: var(--text-tertiary);
}
/* 行悬停反馈：宽表格靠它防止串行 */
:deep(.n-data-table-tr:hover .n-data-table-td) {
  background: var(--overlay-hover);
}

/* 分页器靠左：右下角被固定的 AI 助手悬浮按钮占据，
   实测分页器 (x1261–1361, y866–894) 与 FAB (x1288–1416, y839–876) 直接重叠，
   点击「下一页」会命中 FAB。靠左后彻底避开，也更符合数据表惯例。 */
:deep(.n-data-table__pagination) {
  justify-content: flex-start;
  padding-top: var(--sp-2);
}
:deep(.n-pagination) {
  color: var(--text-secondary);
}
:deep(.n-pagination-item) {
  border-radius: var(--radius-sm);
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