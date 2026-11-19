<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpace, NSpin, NEmpty, NPopconfirm, NSelect,
  NAlert, NInputNumber, NSwitch, NTag, useMessage,
} from 'naive-ui'
import {
  composeProject, listCompositions, exportProject, listExports, downloadExportUrl,
} from '@/api/compose'
import { getJob, listJobs } from '@/api/jobs'
import { getProject } from '@/api/projects'
import type { Composition, ComposePayload, ExportPayload } from '@/types/compose'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- 项目（画幅比决定预览区域形状） ---
const projectQuery = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
})

// --- Compositions ---
const compositionsQuery = useQuery({
  queryKey: ['compositions', projectId],
  queryFn: () => listCompositions(projectId.value),
})

// --- Exports ---
const exportsQuery = useQuery({
  queryKey: ['exports', projectId],
  queryFn: () => listExports(projectId.value),
})

// --- 预览 ---
const previewComposition = ref<Composition | null>(null)
const videoError = ref(false)
const videoKey = ref(0)

/** 合成记录里的 file_path 有两种历史口径（cmp_xxx.mp4 / data/media/cmp_xxx.mp4），
 *  统一裁剪成 media_dir 内的相对路径，避免拼出 /api/media/data/media/... 而 404。 */
function mediaUrl(filePath: string) {
  const clean = filePath.replace(/^\/+/, '')
  const marker = 'data/media/'
  const idx = clean.lastIndexOf(marker)
  return `/api/media/${idx >= 0 ? clean.slice(idx + marker.length) : clean}`
}

function selectPreview(comp: Composition) {
  previewComposition.value = comp
  videoError.value = false
  videoKey.value += 1
}

function retryVideo() {
  videoError.value = false
  videoKey.value += 1
  const el = document.querySelector<HTMLVideoElement>('.video-player')
  el?.load()
}

// 合成记录加载后自动选中最新一条，避免“有记录但预览是空的”
watch(
  () => compositionsQuery.data.value,
  (list) => {
    if (!previewComposition.value && list && list.length > 0) {
      selectPreview(list[0])
    }
  },
  { immediate: true },
)

function formatDuration(sec?: number | null) {
  if (!sec || sec <= 0) return '—'
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

function formatTime(value?: string | null) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

const aspectRatio = computed(() => projectQuery.data.value?.aspect_ratio ?? '9:16')
/** 预览框按项目画幅收敛（竖屏不再左右铺满黑边） */
const ratioValue = computed(() => {
  const map: Record<string, string> = {
    '16:9': '16 / 9', '9:16': '9 / 16', '1:1': '1 / 1', '4:3': '4 / 3', '3:4': '3 / 4',
  }
  return map[aspectRatio.value] ?? '9 / 16'
})

function episodeLabel(episode?: number | null) {
  return episode == null ? '整部' : `第 ${episode} 集`
}

// --- Compose ---
const composeEpisode = ref<number | null>(null)
const composeSubtitle = ref(true)
const composeSubtitleMode = ref<'burn' | 'sidecar'>('burn')

/**
 * 合成任务跟踪：提交后面板常驻展示排队/合成状态。
 * 背景：合成任务与 H3 生成任务共用串行队列（并发=ComfyUI 实例数），
 * 批量生成期间合成可能排队数分钟；只弹一次 toast 会让人以为“点了没反应”。
 */
const composeJobId = ref<string | null>(null)
const composeJobStatus = ref('')
const composeJobError = ref('')
let composePollTimer: ReturnType<typeof setInterval> | null = null

const COMPOSE_STATUS_TEXT: Record<string, string> = {
  queued: '排队中：正在等待前面的生成任务完成，轮到时自动开始',
  chained_wait: '排队中：正在等待前面的生成任务完成',
  running: '合成中：正在拼接镜头、混音并烧录字幕',
  succeeded: '合成完成',
  failed: '合成失败',
  cancelled: '已取消',
  lost: '任务已失联',
}

const composeJobText = computed(() =>
  COMPOSE_STATUS_TEXT[composeJobStatus.value] || composeJobStatus.value,
)
const composeJobActive = computed(() =>
  ['queued', 'running', 'chained_wait'].includes(composeJobStatus.value),
)
const composeJobAlertType = computed<'info' | 'success' | 'error'>(() => {
  if (composeJobStatus.value === 'failed') return 'error'
  if (composeJobStatus.value === 'succeeded') return 'success'
  return 'info'
})

function stopComposePoll() {
  if (composePollTimer) {
    clearInterval(composePollTimer)
    composePollTimer = null
  }
}

async function pollComposeJob() {
  if (!composeJobId.value) return
  try {
    const job = await getJob(composeJobId.value)
    composeJobStatus.value = job.status
    composeJobError.value = job.error_msg || ''
    if (!['queued', 'running', 'chained_wait', 'submission_unknown'].includes(job.status)) {
      stopComposePoll()
      queryClient.invalidateQueries({ queryKey: ['compositions', projectId] })
      if (job.status === 'succeeded') {
        message.success('合成完成，可在右侧记录中预览')
      } else {
        message.error(`合成失败：${job.error_msg || job.error_code || job.status}`)
      }
    }
  } catch {
    // 轮询失败（网络抖动/接口瞬时错误）不终止，等下一轮
  }
}

function trackComposeJob(jobId: string, status = 'queued') {
  composeJobId.value = jobId
  composeJobStatus.value = status
  composeJobError.value = ''
  stopComposePoll()
  composePollTimer = setInterval(pollComposeJob, 3000)
  pollComposeJob()
}

// 刷新页面后恢复跟踪：把仍在排队/合成中的 compose 任务接回面板
onMounted(async () => {
  try {
    const res: any = await listJobs({ project_id: projectId.value, job_type: 'compose', page: 1, page_size: 5 })
    const items = res?.items ?? []
    const active = items.find((j: any) =>
      ['queued', 'running', 'chained_wait', 'submission_unknown'].includes(j.status))
    if (active) trackComposeJob(active.id, active.status)
  } catch {
    // 列表接口不可用时忽略，不影响页面其它功能
  }
})

onUnmounted(stopComposePoll)

const composeMutation = useMutation({
  mutationFn: (payload: ComposePayload) => composeProject(projectId.value, payload),
  onSuccess: (data: any) => {
    lastError.value = ''
    message.success('合成任务已提交')
    if (data?.job_id) trackComposeJob(data.job_id)
    queryClient.invalidateQueries({ queryKey: ['compositions', projectId] })
  },
  onError: (e: any) => {
    const code = e?.code
    let text = ''
    if (code === 'output_missing') {
      text = '存在缺失产物，无法合成：请先在分镜看板补齐全部分镜图与视频'
    } else if (code === 'duration_mismatch_review_required') {
      text = '存在待审核产物，需先处理后再合成：请到分镜看板完成审核'
    } else if (code === 'speaker_mapping_required') {
      text = '缺少角色配音映射，请先配置角色音色映射'
    } else {
      text = `合成失败：${e?.message ?? e}`
    }
    lastError.value = text
    message.error(text)
  },
})

/** 合成阻断判断（仅 UI 层）：本页已跟踪到排队/进行中的合成任务，或已有合成版本在进行中时禁用按钮 */
const hasBlockingIssues = computed(() =>
  composeJobActive.value
  || (compositionsQuery.data.value ?? []).some((c: Composition) => c.status === 'composing'),
)
const blockingReason = computed(() =>
  hasBlockingIssues.value ? '已有合成任务在排队或进行中，请等待完成后再提交' : '',
)
/** 最近一次操作错误：页面内以 NAlert 常驻展示，避免只依赖短暂 toast */
const lastError = ref('')

function handleCompose() {
  composeMutation.mutate({
    episode: composeEpisode.value ?? undefined,
    include_subtitle: composeSubtitle.value,
    subtitle_mode: composeSubtitleMode.value,
  })
}

// --- Export ---
const exportCompositionId = ref<string | null>(null)
const exportFormat = ref('mp4')
const exportProfile = ref<'standard' | 'high_quality'>('standard')

/**
 * 导出任务跟踪：导出完成后明确告知「文件在哪、叫什么、多大、怎么下载」。
 * 此前只弹一次“已提交”，用户看不到导出落盘位置。
 */
const exportJobId = ref<string | null>(null)
const exportJobStatus = ref('')
const exportJobFile = ref('')
const exportJobPath = ref('')
const exportJobDownloadUrl = ref('')
const exportJobError = ref('')
let exportPollTimer: ReturnType<typeof setInterval> | null = null

const EXPORT_STATUS_TEXT: Record<string, string> = {
  queued: '排队中，轮到时自动开始',
  running: '导出中（封装/重编码）',
  succeeded: '导出完成',
  failed: '导出失败',
  cancelled: '已取消',
  lost: '任务已失联',
}
const exportJobText = computed(() => EXPORT_STATUS_TEXT[exportJobStatus.value] || exportJobStatus.value)
const exportJobAlertType = computed<'info' | 'success' | 'error'>(() => {
  if (exportJobStatus.value === 'failed') return 'error'
  if (exportJobStatus.value === 'succeeded') return 'success'
  return 'info'
})

function stopExportPoll() {
  if (exportPollTimer) {
    clearInterval(exportPollTimer)
    exportPollTimer = null
  }
}

async function pollExportJob() {
  if (!exportJobId.value) return
  try {
    const job = await getJob(exportJobId.value)
    exportJobStatus.value = job.status
    exportJobError.value = job.error_msg || ''
    if (!['queued', 'running'].includes(job.status)) {
      stopExportPoll()
      const res: any = job.result || {}
      exportJobFile.value = res.file_name || ''
      exportJobDownloadUrl.value = res.download_url
        || (res.export_id ? `/api/exports/${res.export_id}/download` : '')
      const list: any = await exportsQuery.refetch()
      const items: any[] = (list as any)?.data ?? exportsQuery.data.value ?? []
      const hit = items.find((it: any) => it.file_name && it.file_name === exportJobFile.value)
      exportJobPath.value = hit?.abs_path || hit?.file_path || ''
      if (job.status === 'succeeded') {
        message.success(`导出完成：${exportJobFile.value || '文件已生成'}，可点「下载到本机」保存`)
      } else {
        message.error(`导出失败：${job.error_msg || job.error_code || job.status}`)
      }
    }
  } catch {
    // 轮询失败不终止，等下一轮
  }
}

function trackExportJob(jobId: string) {
  exportJobId.value = jobId
  exportJobStatus.value = 'queued'
  exportJobFile.value = ''
  exportJobPath.value = ''
  exportJobError.value = ''
  stopExportPoll()
  exportPollTimer = setInterval(pollExportJob, 2000)
  pollExportJob()
}

onUnmounted(stopExportPoll)

function formatSize(bytes?: number | null) {
  if (!bytes || bytes <= 0) return ''
  const mb = bytes / 1024 / 1024
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

/** 复制导出文件绝对路径到剪贴板（浏览器不支持时退回选中文本） */
async function copyText(text: string) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    message.success('路径已复制')
  } catch {
    message.warning('复制失败，请手动选中复制')
  }
}

const exportMutation = useMutation({
  mutationFn: (payload: ExportPayload) => exportProject(projectId.value, payload),
  onSuccess: (data: any) => {
    lastError.value = ''
    message.success('导出任务已提交')
    if (data?.job_id) trackExportJob(data.job_id)
    queryClient.invalidateQueries({ queryKey: ['exports', projectId] })
  },
  onError: (e: any) => {
    const code = e?.code
    const text =
      code === 'composition_stale'
        ? '合成版本已失效，请重新合成后再导出'
        : `导出失败：${e?.message ?? e}`
    lastError.value = text
    message.error(text)
  },
})

/** 记录里只显示文件名：服务器绝对路径对用户没意义，完整路径放 title */
function baseName(p?: string | null): string {
  if (!p) return ''
  const parts = p.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || p
}

function handleExport() {
  if (!exportCompositionId.value) {
    message.warning('请选择合成版本')
    return
  }
  exportMutation.mutate({
    composition_id: exportCompositionId.value,
    format: exportFormat.value,
    delivery_profile: exportProfile.value,
  })
}

/** 触发浏览器下载到本机（Windows 默认下载目录）。
 *  用 <a download> 而不是 window.open：后者常被弹窗拦截，表现就是“点了没反应”。 */
function downloadFile(url: string) {
  if (!url) return
  const a = document.createElement('a')
  a.href = url
  a.download = ''  // 文件名由服务端 Content-Disposition 给出
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  message.success('已开始下载，文件保存在本机浏览器下载目录')
}

function handleDownload(exportId: string) {
  downloadFile(downloadExportUrl(exportId))
}

const subtitleModeOptions = [
  { label: '内嵌字幕（烧入画面）', value: 'burn' },
  { label: '外挂字幕（仅输出 SRT）', value: 'sidecar' },
]
const profileOptions = [
  { label: '标准质量', value: 'standard' },
  { label: '高质量', value: 'high_quality' },
]

const compositionOptions = computed(() =>
  (compositionsQuery.data.value ?? [])
    .filter((c: Composition) => !c.stale)
    .map((c: Composition) => ({
      label: `${episodeLabel(c.episode)} · ${formatTime(c.created_at)}`,
      value: c.composition_id,
    }))
)

const compositions = computed(() => compositionsQuery.data.value ?? [])
const exportsList = computed(() => exportsQuery.data.value ?? [])

/*
 * 默认选中最新的可用合成版本。
 * 为什么：只有一个版本时若让用户先手动选，「导出 MP4」会一直处于禁用态，
 * 用户会判断成功能坏了。自动选中不等于自动导出，仍需点「导出 MP4」并二次确认。
 */
watch(
  compositionOptions,
  (opts) => {
    if (!opts.length) return
    const stillValid = opts.some((o) => o.value === exportCompositionId.value)
    if (!stillValid) exportCompositionId.value = opts[0].value
  },
  { immediate: true },
)

function goStoryboard() {
  router.push(`/project/${projectId.value}/storyboard`)
}

function statusTagClass(status: string) {
  if (status === 'done' || status === 'succeeded') return 'is-succeeded'
  if (status === 'failed') return 'is-failed'
  return 'is-running'
}
</script>

<template>
  <div class="compose-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">合成与导出</h1>
      </div>
      <n-space :size="8" align="center">
        <n-tag size="small" round :bordered="false">
          {{ aspectRatio }} · {{ projectQuery.data.value?.name ?? '当前项目' }}
        </n-tag>
        <n-button size="small" quaternary @click="goStoryboard">去分镜看板</n-button>
      </n-space>
    </header>

    <n-alert
      v-if="lastError"
      type="error"
      closable
      class="page-alert"
      @close="lastError = ''"
    >
      {{ lastError }}
    </n-alert>

    <div class="compose-grid">
      <!-- 左：预览 -->
      <section class="col-preview">
        <n-card size="small" title="视频预览" class="panel-card">
          <template #header-extra>
            <span v-if="previewComposition" class="preview-meta">
              {{ episodeLabel(previewComposition.episode) }} ·
              {{ formatDuration(previewComposition.duration) }}
            </span>
            <n-button
              v-if="previewComposition"
              size="tiny"
              quaternary
              @click="retryVideo"
            >重新载入</n-button>
          </template>

          <div v-if="previewComposition?.file_path" class="video-frame" :style="{ '--preview-ratio': ratioValue }">
            <video
              :key="videoKey"
              class="video-player"
              controls
              playsinline
              preload="metadata"
              @error="videoError = true"
            >
              <source :src="mediaUrl(previewComposition.file_path)" type="video/mp4" />
            </video>
          </div>
          <div v-else class="video-frame is-empty" :style="{ '--preview-ratio': ratioValue }">
            <n-empty
              size="small"
              :description="compositions.length ? '从右侧合成记录中选择一个版本预览' : '还没有合成版本，先提交一次合成'"
            />
          </div>

          <n-alert v-if="videoError" type="error" class="video-error" closable @close="videoError = false">
            视频加载失败：文件可能被移动或服务异常。
            <n-button text type="primary" size="tiny" @click="retryVideo">重试</n-button>
          </n-alert>
        </n-card>
      </section>

      <!-- 中：合成设置 + 导出 -->
      <section class="col-forms">

        <n-card size="small" title="合成设置" class="panel-card">
          <div class="form-grid">
            <div class="form-item">
              <span class="field-label">集数</span>
              <n-input-number
                v-model:value="composeEpisode"
                :min="1"
                placeholder="留空则合成全部"
                clearable
                style="width: 100%"
              />
            </div>
            <div class="form-item">
              <span class="field-label">字幕</span>
              <div class="inline-control">
                <n-switch v-model:value="composeSubtitle" size="small" />
                <n-select
                  v-model:value="composeSubtitleMode"
                  :options="subtitleModeOptions"
                  size="small"
                  class="inline-select"
                  :disabled="!composeSubtitle"
                />
              </div>
            </div>
          </div>

          <div class="form-actions">
            <n-button
              type="primary"
              :loading="composeMutation.isPending.value"
              :disabled="hasBlockingIssues"
              @click="handleCompose"
            >开始合成</n-button>
            <span class="form-hint">{{ blockingReason || '合成使用本地 FFmpeg，不占用 ComfyUI 算力' }}</span>
          </div>

          <n-alert v-if="composeJobId" :type="composeJobAlertType" class="job-alert" closable
                   @close="composeJobId = null; stopComposePoll()">
            合成任务 {{ composeJobId }}：{{ composeJobText }}
            <span v-if="composeJobError">（{{ composeJobError }}）</span>
          </n-alert>
        </n-card>

        <n-card size="small" title="导出" class="panel-card">
          <div class="form-grid">
            <div class="form-item">
              <span class="field-label">合成版本</span>
              <n-select
                v-model:value="exportCompositionId"
                :options="compositionOptions"
                placeholder="选择非失效的合成版本"
              />
            </div>
            <div class="form-item">
              <span class="field-label">导出质量</span>
              <n-select v-model:value="exportProfile" :options="profileOptions" />
            </div>
          </div>
          <div class="form-actions">
            <n-popconfirm @positive-click="handleExport">
              <template #trigger>
                <n-button
                  type="primary"
                  :loading="exportMutation.isPending.value"
                  :disabled="!exportCompositionId"
                  :title="exportCompositionId ? '导出当前合成版本为 MP4' : '请先选择一个已完成的合成版本'"
                >导出 MP4</n-button>
              </template>
              确认使用当前选择的合成版本导出 MP4？
            </n-popconfirm>
            <span class="form-hint">导出先写入服务器 media 目录，完成后点「下载到本机」保存到本机；右侧记录可再次下载</span>
          </div>

          <n-alert v-if="exportJobId" :type="exportJobAlertType" class="job-alert" closable
                   @close="exportJobId = null; stopExportPoll()">
            <div>导出任务 {{ exportJobId }}：{{ exportJobText }}</div>
            <div v-if="exportJobFile" class="path-row">
              <span class="path-label">文件</span>
              <code class="path-text" :title="exportJobPath || exportJobFile">{{ exportJobFile }}</code>
            </div>
            <div v-if="exportJobPath" class="path-row">
              <span class="path-label">位置</span>
              <code class="path-text" :title="exportJobPath">{{ exportJobPath }}</code>
              <n-button size="tiny" quaternary @click="copyText(exportJobPath)">复制</n-button>
            </div>
            <div v-if="exportJobDownloadUrl && exportJobStatus === 'succeeded'" class="path-row">
              <n-button type="primary" size="tiny" @click="downloadFile(exportJobDownloadUrl)">
                下载到本机
              </n-button>
              <span class="path-hint">保存到本机浏览器下载目录</span>
            </div>
            <div v-if="exportJobError" class="path-row">{{ exportJobError }}</div>
          </n-alert>
        </n-card>
      </section>

      <!-- 右：记录 -->
      <aside class="col-records">
        <n-card size="small" title="合成记录" class="panel-card">
          <template #header-extra>
            <n-button size="tiny" quaternary :loading="compositionsQuery.isFetching.value"
                      @click="() => compositionsQuery.refetch()">刷新</n-button>
          </template>

          <n-spin v-if="compositionsQuery.isLoading.value" size="small" />
          <n-empty v-else-if="compositions.length === 0" size="small" description="暂无合成记录" style="padding: 16px 0" />
          <div v-else class="record-list">
            <div
              v-for="comp in compositions"
              :key="comp.composition_id"
              class="record-item"
              :class="{
                stale: comp.stale,
                active: previewComposition?.composition_id === comp.composition_id,
              }"
              @click="selectPreview(comp)"
            >
              <div class="record-head">
                <span class="record-title">{{ episodeLabel(comp.episode) }}</span>
                <span class="status-tag" :class="statusTagClass(comp.status)">
                  {{ comp.status === 'composing' ? '合成中' : comp.status === 'done' ? '已完成' : comp.status }}
                </span>
              </div>
              <div class="record-meta">
                <span>{{ formatDuration(comp.duration) }}</span>
                <span class="dot">·</span>
                <span>{{ formatTime(comp.created_at) }}</span>
                <span v-if="comp.stale" class="status-tag is-stale">已失效</span>
                <n-button
                  size="tiny"
                  text
                  type="primary"
                  class="record-action"
                  @click.stop="selectPreview(comp)"
                >预览</n-button>
              </div>
            </div>
          </div>
        </n-card>

        <n-card size="small" title="导出记录" class="panel-card">
          <n-spin v-if="exportsQuery.isLoading.value" size="small" />
          <n-empty v-else-if="exportsList.length === 0" size="small" description="暂无导出记录" style="padding: 16px 0" />
          <div v-else class="record-list">
            <div v-for="exp in exportsList" :key="exp.id" class="record-item is-plain">
              <div class="record-head">
                <span class="record-title">{{ exp.file_name ?? '导出文件' }}</span>
                <span class="status-tag" :class="statusTagClass(exp.status)">
                  {{ exp.status === 'exporting' ? '处理中' : exp.status === 'done' ? '已完成' : '失败' }}
                </span>
              </div>
              <div class="record-meta">
                <span>{{ formatTime(exp.created_at) }}</span>
                <template v-if="exp.size">
                  <span class="dot">·</span>
                  <span>{{ formatSize(exp.size) }}</span>
                </template>
                <n-button
                  v-if="exp.status === 'done'"
                  size="tiny"
                  secondary
                  class="record-action"
                  @click.stop="handleDownload(exp.id)"
                >下载</n-button>
              </div>
              <div
                v-if="(exp.abs_path || exp.file_path) && baseName(exp.abs_path || exp.file_path) !== exp.file_name"
                class="record-path"
                :title="exp.abs_path || exp.file_path"
              >
                {{ baseName(exp.abs_path || exp.file_path) }}
              </div>
            </div>
          </div>
        </n-card>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.compose-page {
  padding: var(--sp-6);
  max-width: 1440px;
  margin: 0 auto;
  min-height: calc(100vh - var(--app-header-height, 56px));
  box-sizing: border-box;
}
.page-header { margin: 0 0 var(--sp-4); }
.page-alert { margin-bottom: var(--sp-4); }

.compose-grid {
  display: grid;
  grid-template-columns: minmax(280px, 340px) minmax(0, 1fr) minmax(300px, 380px);
  gap: var(--sp-4);
  /* 三栏等高：预览最高，记录列吃掉剩余高度，底部不再参差 */
  align-items: stretch;
}
.col-preview,
.col-forms,
.col-records { display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0; }
.col-forms .panel-card { flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; }
.col-forms :deep(.n-card__content) { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.col-forms .form-actions { margin-top: auto; }
.col-records { min-height: 0; }
.col-records .panel-card { flex: 1 1 0; min-height: 0; display: flex; flex-direction: column; }
.col-records :deep(.n-card__content) { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.col-records .record-list { flex: 1; min-height: 0; overflow-y: auto; }

.panel-card {
  --n-color: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.panel-card :deep(.n-card-header) { padding-bottom: var(--sp-2); }
.panel-card :deep(.n-card__content) { padding-top: 0; }

.preview-meta { font-size: 13px; color: var(--text-secondary); margin-right: var(--sp-2); }

.video-frame {
  width: 100%;
  aspect-ratio: var(--preview-ratio, 9 / 16);
  min-height: 180px;
  background: var(--media-backdrop);
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.video-frame.is-empty { background: var(--bg-elevated); }
.video-player {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: var(--media-backdrop);
  display: block;
}
.video-error { margin-top: var(--sp-3); }

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--sp-4);
  margin-bottom: var(--sp-4);
}
.form-item { min-width: 0; }
.inline-control { display: flex; align-items: center; gap: var(--sp-2); }
.inline-select { flex: 1 1 auto; min-width: 0; }
.field-label {
  display: block;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: var(--sp-2);
}
.form-actions { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.form-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
/* 禁用态也要看得见：灰底 + 描边，别只剩一行几乎与背景同色的字 */
.form-actions :deep(.n-button--disabled),
.form-actions :deep(.n-button:disabled) {
  opacity: 1;
  color: var(--text-tertiary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
}
.form-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.job-alert { margin-top: var(--sp-3); }

.record-list { display: flex; flex-direction: column; gap: var(--sp-2); }
.record-item {
  padding: 10px 12px;
  background: var(--bg-elevated);
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}
.record-item.is-plain { cursor: default; }
.record-item:hover { border-color: var(--border-strong); }
.record-item.active { border-color: var(--brand-primary); background: var(--brand-soft); }
.record-item.stale { opacity: 0.55; }
.record-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-bottom: 6px;
}
.record-title {
  font-size: 14px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.record-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
  flex-wrap: wrap;
}
.record-meta .dot { color: var(--text-tertiary); }
.record-action { margin-left: auto; }
.record-path {
  margin-top: var(--sp-1);
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.path-hint { font-size: 11px; color: var(--text-tertiary); }
.path-row { display: flex; align-items: center; gap: 6px; margin-top: 4px; min-width: 0; }
.path-label { flex: none; font-size: 12px; color: var(--text-secondary); }
.path-text {
  flex: 1 1 auto;
  min-width: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 1100px) {
  .compose-grid { grid-template-columns: minmax(260px, 300px) minmax(0, 1fr); }
  .col-records {
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-items: start;
  }
}
@media (max-width: 768px) {
  .compose-grid { grid-template-columns: minmax(0, 1fr); }
  .col-records { grid-template-columns: minmax(0, 1fr); }
  .video-frame { max-width: 360px; }
}
@media (max-width: 768px) {
  .compose-page { padding: var(--sp-4); }
  .video-frame { max-width: 100%; }
}
</style>
