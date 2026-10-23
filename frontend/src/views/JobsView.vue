<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpace, NSpin, NEmpty, NTag, NDataTable, NSelect,
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

const typeOptions = [
  { label: '全部类型', value: '' },
  { label: '图像', value: 'generate_keyframe' },
  { label: '视频', value: 'generate_clip' },
  { label: '语音', value: 'generate_audio' },
  { label: 'BGM', value: 'generate_bgm' },
  { label: 'SFX', value: 'generate_sfx' },
  { label: '解析', value: 'parse' },
  { label: '合成', value: 'compose' },
  { label: '导出', value: 'export' },
]
const statusOptions = [
  { label: '全部状态', value: '' },
  { label: '排队中', value: 'queued' },
  { label: '执行中', value: 'running' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
  { label: '丢失', value: 'lost' },
  { label: '已取消', value: 'cancelled' },
]

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
let eventSource: EventSource | null = null

onMounted(() => {
  try {
    eventSource = new EventSource('/api/jobs/events')
    eventSource.onmessage = () => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      queryClient.invalidateQueries({ queryKey: ['jobStats'] })
    }
    eventSource.onerror = () => {
      // SSE 断线降级轮询（refetchInterval 已配置）
    }
  } catch {
    // EventSource 不可用时依赖轮询
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

// --- Table ---
const statusColors: Record<string, string> = {
  queued: '#3B82F6', running: '#3B82F6', succeeded: '#22C55E',
  failed: '#EF4444', lost: '#EF4444', cancelled: '#9CA3AF',
}
const statusLabels: Record<string, string> = {
  queued: '排队中', running: '执行中', succeeded: '成功',
  failed: '失败', lost: '丢失', cancelled: '已取消',
}
const typeLabels: Record<string, string> = {
  generate_keyframe: '图像', generate_clip: '视频', generate_audio: '语音',
  generate_bgm: 'BGM', generate_sfx: 'SFX', parse: '解析',
  compose: '合成', export: '导出',
}

function isEnvError(job: Job): boolean {
  // 环境类失败：连接超时、ComfyUI 不可达等；配置类：参数错误、工作流缺失等
  return job.retryable === true
}

const columns = computed<DataTableColumns<Job>>(() => [
  { title: 'ID', key: 'id', width: 120, ellipsis: { tooltip: true } },
  { title: '类型', key: 'job_type', width: 80,
    render: (row) => typeLabels[row.job_type] || row.job_type },
  { title: '状态', key: 'status', width: 90,
    render: (row) =>
      h(NTag, { size: 'small', round: true, color: { color: statusColors[row.status], textColor: '#fff' } },
        { default: () => statusLabels[row.status] || row.status }) },
  { title: '目标', key: 'target_id', width: 120, ellipsis: { tooltip: true } },
  { title: '尝试', key: 'attempts', width: 60 },
  { title: '创建时间', key: 'created_at', width: 160,
    render: (row) => new Date(row.created_at).toLocaleString() },
  { title: '操作', key: 'actions', width: 120,
    render: (row) => {
      if (row.status === 'failed' || row.status === 'lost') {
        if (isEnvError(row)) {
          return h(NButton, { size: 'tiny', type: 'primary', quaternary: true,
            onClick: () => retryMutation.mutate(row.id) }, { default: () => '重试' })
        }
        return h(NTooltip, null, {
          trigger: () => h(NButton, { size: 'tiny', quaternary: true }, { default: () => '查看指引' }),
          default: () => row.error?.message || '配置类错误，请检查参数',
        })
      }
      if (row.status === 'queued' || row.status === 'running') {
        return h(NButton, { size: 'tiny', type: 'error', quaternary: true,
          onClick: () => cancelMutation.mutate(row.id) }, { default: () => '取消' })
      }
      return '-'
    },
  },
])

import { h } from 'vue'
</script>

<template>
  <div class="jobs-page">
    <!-- Stats cards -->
    <div class="stats-row">
      <n-card class="stat-card">
        <n-statistic label="今日成功" :value="statsQuery.data.value?.today_succeeded ?? 0" />
      </n-card>
      <n-card class="stat-card">
        <n-statistic label="今日失败" :value="statsQuery.data.value?.today_failed ?? 0" />
      </n-card>
      <n-card class="stat-card">
        <n-statistic label="进行中" :value="statsQuery.data.value?.in_progress ?? 0" />
      </n-card>
      <n-card class="stat-card">
        <n-statistic label="平均耗时(秒)" :value="Math.round(statsQuery.data.value?.avg_duration_seconds ?? 0)" />
      </n-card>
    </div>

    <!-- Filters -->
    <div class="filters">
      <n-space align="center">
        <n-select v-model:value="typeFilter" :options="typeOptions" size="small" style="width: 140px" clearable />
        <n-select v-model:value="statusFilter" :options="statusOptions" size="small" style="width: 140px" clearable />
      </n-space>
    </div>

    <!-- Table -->
    <n-spin v-if="jobsQuery.isLoading.value" class="page-loading" />
    <n-empty v-else-if="jobsQuery.data.value?.items?.length === 0" description="暂无任务" />
    <n-data-table
      v-else
      :columns="columns"
      :data="jobsQuery.data.value?.items || []"
      :bordered="false"
      :single-line="false"
      size="small"
      :pagination="{
        page: page,
        pageSize: pageSize,
        itemCount: jobsQuery.data.value?.total ?? 0,
        onUpdatePage: (p: number) => { page = p },
        showSizePicker: false,
      }"
    />
  </div>
</template>

<style scoped>
.jobs-page { padding: 16px; min-height: calc(100vh - 56px); }
.stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }
.stat-card { background: #18181D; border: 1px solid #26262E; border-radius: 12px; }
.filters { margin-bottom: 16px; }
.page-loading { display: flex; justify-content: center; padding: 80px 0; }
</style>
