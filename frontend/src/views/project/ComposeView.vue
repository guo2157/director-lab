<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpin, NEmpty, NPopconfirm, NSelect,
  NInputNumber, NSwitch, useMessage,
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
/**
 * 画幅数值比（宽 / 高）。
 * 只有 aspect-ratio 一个约束时，容器宽度仍会铺满整列，竖屏视频左右就留出大片黑边；
 * 用 max-width = 可用高度 × 宽高比，让宽度反过来跟着高度收，画面才不会嵌在宽黑框里。
 */
const ratioNumber = computed(() => {
  const map: Record<string, number> = {
    '16:9': 16 / 9, '9:16': 9 / 16, '1:1': 1, '4:3': 4 / 3, '3:4': 3 / 4,
  }
  return map[aspectRatio.value] ?? 9 / 16
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

type TagKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'

/** 记录状态 → 统一状态标签语义（合成 composing / 导出 exporting 都映射到 running） */
function statusKind(status: string, stale = false): TagKind {
  if (stale) return 'stale'
  if (status === 'done' || status === 'succeeded') return 'succeeded'
  if (status === 'failed') return 'failed'
  return 'running'
}

/** 当前预览版本是否可播放（决定视频框是否亮起品牌光晕） */
const previewReady = computed(
  () => !!previewComposition.value?.file_path && previewComposition.value?.status === 'done',
)

/** 导出完成后的一行式结果：文件名 + 下载入口，不铺成三行路径块 */
const exportDone = computed(() => exportJobStatus.value === 'succeeded' && !!exportJobDownloadUrl.value)
</script>

<template>
  <div class="compose-page">
    <!-- 左侧「制作流程」栏已高亮 06 合成与导出，页内不再重复同名大标题。
         H1 保留为 .visually-hidden：删掉可见标题后无障碍树仍需标题层级。 -->
    <h1 class="visually-hidden">合成与导出</h1>

    <header class="cx-bar">
      <span class="cx-bar-project">
        <span class="cx-bar-ratio">{{ aspectRatio }}</span>
        {{ projectQuery.data.value?.name ?? '当前项目' }}
      </span>
      <n-button size="small" quaternary @click="goStoryboard">去分镜看板</n-button>
    </header>

    <!-- 错误条：一条就够，不再用整块 NAlert 压住页面 -->
    <div v-if="lastError" class="cx-error" role="alert">
      <span class="cx-error-text">{{ lastError }}</span>
      <button type="button" class="cx-link" @click="lastError = ''">知道了</button>
    </div>

    <div class="cx-main" :style="{ '--cx-ratio-num': ratioNumber }">
      <!-- 左：成片预览。整页唯一的主角，就绪时给品牌描边，其余一切安静 -->
      <section class="cx-stage" :class="{ 'is-ready': previewReady }">
        <div class="cx-stage-head">
          <span class="cx-stage-title">成片预览</span>
          <span v-if="previewComposition" class="cx-stage-meta">
            {{ episodeLabel(previewComposition.episode) }}
            <template v-if="previewComposition.duration"> · {{ formatDuration(previewComposition.duration) }}</template>
          </span>
          <button
            v-if="previewComposition?.file_path"
            type="button"
            class="cx-link cx-stage-reload"
            @click="retryVideo"
          >重新载入</button>
        </div>

        <div v-if="previewComposition?.file_path" class="cx-frame" :style="{ '--cx-ratio': ratioValue }">
          <video
            :key="videoKey"
            class="cx-video"
            controls
            playsinline
            preload="metadata"
            @error="videoError = true"
          >
            <source :src="mediaUrl(previewComposition.file_path)" type="video/mp4" />
          </video>
        </div>
        <div v-else class="cx-frame is-empty" :style="{ '--cx-ratio': ratioValue }">
          <div class="cx-frame-empty">
            <p class="cx-frame-empty-title">{{ compositions.length ? '选一个版本预览' : '还没有成片' }}</p>
            <p class="cx-frame-empty-hint">
              {{ compositions.length ? '点右侧「产出记录」里的任一合成版本' : '先在右侧提交一次合成' }}
            </p>
          </div>
        </div>

        <p v-if="videoError" class="cx-frame-error">
          视频加载失败：文件可能被移动或服务异常。
          <button type="button" class="cx-link" @click="retryVideo">重试</button>
        </p>
      </section>

      <!-- 右：01 合成 → 02 导出。序号成立，这两步确有先后 -->
      <section class="cx-flow">
        <div class="cx-step">
          <div class="cx-step-head">
            <span class="cx-step-no">01</span>
            <h2 class="cx-step-title">合成</h2>
            <span class="cx-step-note">本地 FFmpeg，不占用 ComfyUI 算力</span>
          </div>

          <div class="cx-fields">
            <label class="cx-field">
              <span class="cx-field-label">集数</span>
              <n-input-number
                v-model:value="composeEpisode"
                :min="1"
                placeholder="留空 = 全部"
                clearable
                size="small"
              />
            </label>
            <div class="cx-field">
              <span class="cx-field-label">字幕</span>
              <div class="cx-field-inline">
                <n-switch v-model:value="composeSubtitle" size="small" />
                <n-select
                  v-model:value="composeSubtitleMode"
                  :options="subtitleModeOptions"
                  size="small"
                  :disabled="!composeSubtitle"
                />
              </div>
            </div>
          </div>

          <div class="cx-step-foot">
            <n-button
              type="primary"
              size="small"
              class="btn-brand"
              :loading="composeMutation.isPending.value"
              :disabled="hasBlockingIssues"
              @click="handleCompose"
            >开始合成</n-button>
            <span v-if="blockingReason" class="cx-hint is-warn">{{ blockingReason }}</span>
          </div>

          <div v-if="composeJobId" class="cx-job" :class="composeJobAlertType">
            <span class="cx-job-text">{{ composeJobText }}</span>
            <span v-if="composeJobError" class="cx-job-err">{{ composeJobError }}</span>
            <button type="button" class="cx-link" @click="composeJobId = null; stopComposePoll()">收起</button>
          </div>
        </div>

        <div class="cx-step">
          <div class="cx-step-head">
            <span class="cx-step-no">02</span>
            <h2 class="cx-step-title">导出</h2>
            <span class="cx-step-note">先落服务器，再下载到本机</span>
          </div>

          <div class="cx-fields">
            <label class="cx-field">
              <span class="cx-field-label">合成版本</span>
              <n-select
                v-model:value="exportCompositionId"
                :options="compositionOptions"
                size="small"
                placeholder="选择未失效的合成版本"
              />
            </label>
            <label class="cx-field">
              <span class="cx-field-label">质量</span>
              <n-select v-model:value="exportProfile" :options="profileOptions" size="small" />
            </label>
          </div>

          <div class="cx-step-foot">
            <n-popconfirm @positive-click="handleExport">
              <template #trigger>
                <n-button
                  type="primary"
                  size="small"
                  class="btn-brand"
                  :loading="exportMutation.isPending.value"
                  :disabled="!exportCompositionId"
                  :title="exportCompositionId ? '导出当前合成版本为 MP4' : '请先选择一个已完成的合成版本'"
                >导出 MP4</n-button>
              </template>
              确认使用当前选择的合成版本导出 MP4？
            </n-popconfirm>
            <span class="cx-hint">完成后可在下方产出记录里再次下载</span>
          </div>

          <div v-if="exportJobId" class="cx-job" :class="exportJobAlertType">
            <span class="cx-job-text">{{ exportJobText }}</span>
            <template v-if="exportDone">
              <span class="cx-job-file" :title="exportJobPath || exportJobFile">{{ exportJobFile }}</span>
              <button type="button" class="cx-link" @click="downloadFile(exportJobDownloadUrl)">下载到本机</button>
            </template>
            <span v-if="exportJobError" class="cx-job-err">{{ exportJobError }}</span>
            <button
              v-if="exportJobPath"
              type="button"
              class="cx-link is-quiet"
              @click="copyText(exportJobPath)"
            >复制路径</button>
            <button type="button" class="cx-link is-quiet" @click="exportJobId = null; stopExportPoll()">收起</button>
          </div>
        </div>
        <!-- 产出记录：合成与导出合成一份「产出了什么」，一行一条，不再各占一张卡。
             与 01/02 同列，右列不再在两张卡之后留一大片空白。 -->
        <section class="cx-out">
          <div class="cx-out-head">
            <h2 class="cx-out-title">产出记录</h2>
            <span class="cx-out-count">合成 {{ compositions.length }} · 导出 {{ exportsList.length }}</span>
            <n-button
              size="tiny"
              quaternary
              :loading="compositionsQuery.isFetching.value || exportsQuery.isFetching.value"
              @click="compositionsQuery.refetch(); exportsQuery.refetch()"
            >刷新</n-button>
          </div>

          <div v-if="compositionsQuery.isLoading.value || exportsQuery.isLoading.value" class="cx-out-loading">
            <n-spin size="small" />
          </div>

          <template v-else>
            <div v-if="!compositions.length && !exportsList.length" class="cx-out-empty">
              <n-empty size="small" description="还没有成片或导出文件" />
            </div>

            <ul v-else class="cx-rows">
              <li
                v-for="comp in compositions"
                :key="comp.composition_id"
                class="cx-row"
                :class="{
                  'is-active': previewComposition?.composition_id === comp.composition_id,
                  'is-stale': comp.stale,
                }"
              >
                <span class="cx-row-kind">合成</span>
                <button type="button" class="cx-row-main" @click="selectPreview(comp)">
                  <span class="cx-row-name">{{ episodeLabel(comp.episode) }}</span>
                  <span class="cx-row-meta">
                    <span>{{ formatDuration(comp.duration) }}</span>
                    <span>{{ formatTime(comp.created_at) }}</span>
                  </span>
                </button>
                <span class="status-tag" :class="`is-${statusKind(comp.status, comp.stale)}`">
                  {{ comp.stale ? '已失效' : comp.status === 'composing' ? '合成中' : comp.status === 'done' ? '已完成' : '失败' }}
                </span>
                <button
                  type="button"
                  class="cx-link cx-row-act"
                  :disabled="comp.stale"
                  @click="selectPreview(comp)"
                >预览</button>
              </li>

              <li v-for="exp in exportsList" :key="exp.id" class="cx-row">
                <span class="cx-row-kind is-export">导出</span>
                <div class="cx-row-main is-static">
                  <span class="cx-row-name is-mono" :title="exp.file_name">{{ exp.file_name ?? '导出文件' }}</span>
                  <span class="cx-row-meta">
                    <template v-if="exp.size">{{ formatSize(exp.size) }}</template>
                    <span>{{ formatTime(exp.created_at) }}</span>
                  </span>
                </div>
                <span class="status-tag" :class="`is-${statusKind(exp.status)}`">
                  {{ exp.status === 'exporting' ? '处理中' : exp.status === 'done' ? '已完成' : '失败' }}
                </span>
                <button
                  v-if="exp.status === 'done'"
                  type="button"
                  class="cx-dl"
                  @click="handleDownload(exp.id)"
                >下载</button>
                <span v-else class="cx-row-act"></span>
              </li>
            </ul>
          </template>
        </section>
      </section>

    </div>

  </div>
</template>

<style scoped>
.compose-page {
  padding: var(--sp-6) var(--sp-8) var(--sp-12);
  max-width: 1440px;
  margin: 0 auto;
  min-height: calc(100vh - var(--app-header-height, 64px));
  box-sizing: border-box;
}

/* 工具条：只留项目标识与回退，右对齐，不占一整行高度 */
.cx-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
  min-width: 0;
}
.cx-bar-project {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  font-size: 13px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cx-bar-ratio {
  flex: none;
  padding: 1px 8px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
}

/* 错误条：单行、贴边、不抢焦点 */
.cx-error {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-4);
  font-size: 13px;
  color: var(--danger-text);
  background: var(--danger-soft);
  border: 1px solid var(--danger-line);
  border-radius: var(--radius);
}
.cx-error-text { flex: 1; min-width: 0; }

/* 主区：预览 + 流水线。预览列宽度直接跟着画幅走 ——
   竖屏项目收成窄列，画面贴着列边，不再在宽列里嵌一条窄视频、两侧各留一大块黑。 */
.cx-main {
  display: grid;
  grid-template-columns:
    minmax(240px, min(40%, calc((100vh - 300px) * var(--cx-ratio-num, 0.5625))))
    minmax(340px, 1fr);
  gap: var(--sp-6);
  /* 两列等高：右列拉到与左侧预览同高，记录吃满剩余高度，右列底部不再留一大片空白 */
  align-items: stretch;
}

/* ── 成片预览 ── */
.cx-stage {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
}
.cx-stage-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  min-width: 0;
}
.cx-stage-title {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.008em;
  color: var(--text-primary);
}
.cx-stage-meta {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-tertiary);
}
.cx-stage-reload { margin-left: auto; }

.cx-frame {
  width: 100%;
  /* 宽度跟着高度收：竖屏项目不再在宽容器里嵌一条窄画面 */
  max-width: calc(calc(100vh - 300px) * var(--cx-ratio-num, 0.5625));
  aspect-ratio: var(--cx-ratio, 9 / 16);
  max-height: calc(100vh - 300px);
  margin: 0 auto;
  background: var(--media-backdrop);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: border-color var(--transition-base);
}
/* 就绪：唯一被强调的元素，只用描边，不加外发光——光晕在黑色画框上会糊成一圈 */
.cx-stage.is-ready .cx-frame {
  border-color: var(--brand-line);
}
.cx-frame.is-empty {
  background: var(--bg-panel);
  border-style: dashed;
}
.cx-frame-empty {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-6);
  text-align: center;
}
.cx-frame-empty-title {
  margin: 0;
  font-size: 14px;
  color: var(--text-secondary);
}
.cx-frame-empty-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.cx-video {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: var(--media-backdrop);
  display: block;
}
.cx-frame-error {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger-text);
}

/* ── 01 合成 / 02 导出 ──
   两列排：序号 01→02 天然是左→右的阅读顺序，两张卡并排，
   比竖着堆成一条窄柱更能用满预览列右侧的宽度；产出记录跨满整行。 */
.cx-flow {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  grid-template-rows: auto minmax(0, 1fr);
  gap: var(--sp-4);
  /* 不写 align-items:start —— 它会让第二行回到内容高度，
     产出记录就吃不满右列剩余空间，底部又出现空白。 */
  min-width: 0;
  min-height: 0;
}
.cx-step {
  padding: var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-sheen);
}
.cx-step-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  min-width: 0;
}
/* 序号是真实步骤顺序（先合成后导出），不是装饰 */
.cx-step-no {
  flex: none;
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--brand-primary);
}
.cx-step-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.008em;
  color: var(--text-primary);
}
.cx-step-note {
  min-width: 0;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cx-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.cx-field { display: block; min-width: 0; }
.cx-field-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--text-tertiary);
}
.cx-field-inline {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}
.cx-field-inline :deep(.n-select) { flex: 1 1 auto; min-width: 0; }

.cx-step-foot {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.cx-hint {
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
.cx-hint.is-warn { color: var(--state-warning); }

/* 任务状态：一行文字 + 行内动作，不再用整块 alert */
.cx-job {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  font-size: 12px;
  line-height: 1.5;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  min-width: 0;
}
.cx-job::before {
  content: '';
  flex: none;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--brand-primary);
}
.cx-job.success::before { background: var(--state-success); }
.cx-job.error::before { background: var(--danger); }
.cx-job.error { border-color: var(--danger-line); }
.cx-job-text { color: var(--text-secondary); }
.cx-job-file {
  min-width: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cx-job-err { color: var(--danger-text); }
.cx-job .cx-link { margin-left: auto; }

/* 行内文字按钮：全页统一这一种，不再混用 n-button text / 蓝色链接 */
.cx-link {
  padding: 0;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.5;
  color: var(--brand-hover);
  background: none;
  border: 0;
  cursor: pointer;
  white-space: nowrap;
}
.cx-link:hover { color: var(--brand-mint); }
.cx-link:disabled { color: var(--text-disabled); cursor: not-allowed; }
.cx-link.is-quiet { color: var(--text-tertiary); }
.cx-link.is-quiet:hover { color: var(--text-secondary); }

/* ── 产出记录（右列第三段，跨满两栏） ── */
.cx-out {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-top: var(--sp-2);
  padding-top: var(--sp-4);
  border-top: 1px solid var(--border-subtle);
}
.cx-out-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  margin-bottom: var(--sp-2);
}
.cx-out-title {
  margin: 0;
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.008em;
  color: var(--text-primary);
}
.cx-out-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
}
.cx-out-head :deep(.n-button) { margin-left: auto; }
.cx-out-loading { padding: var(--sp-6); display: flex; justify-content: center; }
.cx-out-empty { padding: var(--sp-6) 0; }

.cx-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}
/* 一行一条：类型 · 名称+元信息 · 状态 · 动作。用分隔线而非卡片，密度更高 */
.cx-row {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr) auto 64px;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-2);
  border-bottom: 1px solid var(--border-subtle);
  transition: background var(--transition-fast);
}
.cx-row:hover { background: var(--overlay-hover); }
.cx-row.is-active { background: var(--overlay-selected); }
.cx-row.is-stale { opacity: 0.55; }

.cx-row-kind {
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
}
.cx-row-kind.is-export { color: var(--brand-deep); }

.cx-row-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 0;
  text-align: left;
  font-family: inherit;
  background: none;
  border: 0;
  cursor: pointer;
}
.cx-row-main.is-static { cursor: default; }
.cx-row-name {
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cx-row-name.is-mono { font-family: var(--font-mono); font-size: 12px; }
.cx-row-meta {
  display: flex;
  gap: var(--sp-2);
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  white-space: nowrap;
}
.cx-row-act { justify-self: end; text-align: right; }
.cx-dl {
  justify-self: end;
  padding: 3px var(--sp-3);
  font-family: inherit;
  font-size: 12px;
  line-height: 18px;
  color: var(--text-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.cx-dl:hover {
  color: var(--brand-hover);
  border-color: var(--brand-line);
}

@media (max-width: 1100px) {
  .cx-main { grid-template-columns: minmax(0, 1fr); }
  .cx-frame { max-width: 100%; max-height: 60vh; }
}
@media (max-width: 768px) {
  .compose-page { padding: var(--sp-4) var(--sp-4) var(--sp-10); }
  /* 窄屏 01/02 竖排：两列各 200px 会把下拉与按钮挤到换行 */
  .cx-flow {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto;
  }
  .cx-out { grid-column: 1; }
  .cx-rows { overflow-y: visible; }
  .cx-row { grid-template-columns: 36px minmax(0, 1fr) auto; row-gap: var(--sp-1); }
  .cx-row-act,
  .cx-dl { grid-column: 2 / -1; justify-self: start; }
  .cx-step-note { display: none; }
}
</style>
