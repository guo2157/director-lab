<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, NEmpty, useDialog, useMessage } from 'naive-ui'
import {
  confirmParseVersion,
  getCurrentScript,
  getParseStatus,
  getProject,
  parseScript,
  retryParse,
  runScriptAgent,
} from '@/api/projects'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { getJob } from '@/api/jobs'
import type { EpisodeParseStatusItem, Scene, Shot } from '@/types/script'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import FlowNav from '@/components/FlowNav.vue'
import AudioPlanPanel from '@/components/AudioPlanPanel.vue'

const route = useRoute()
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const queryClient = useQueryClient()
const flow = useScriptFlowStore()

const projectId = computed(() => String(route.params.id ?? ''))

/* 项目切换时重置流程状态（防止其他项目残留的剧本/评估状态误显示对号） */
watch(
  projectId,
  (id) => {
    if (id) flow.ensureProject(id)
  },
  { immediate: true },
)

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) {
    const m = (e as { message?: string }).message
    if (m) return m
  }
  return e instanceof Error ? e.message : '请求失败'
}

/* ---------- 本地扩展类型 ---------- */
interface SceneWithShots extends Scene {
  shots?: Shot[]
}
interface ParseJobResult {
  parse_version_id?: string
  characters?: Array<{ name: string; description?: string }>
  props?: Array<{ name: string; description?: string }>
  scenes?: Array<{ episode?: number; title: string; description?: string }>
  shots?: Array<Record<string, unknown>>
}

/* ---------- 项目信息 ---------- */
const { data: projectData } = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
  enabled: computed(() => !!projectId.value),
})
const project = computed(() => projectData.value)

/* ---------- 剧本版本（刷新恢复） ---------- */
const { data: scriptData } = useQuery({
  queryKey: computed(() => ['currentScript', projectId.value]),
  queryFn: () => getCurrentScript(projectId.value),
  enabled: computed(() => !!projectId.value),
})
watch(
  () => scriptData.value,
  (sv) => {
    if (sv?.exists && sv.script_version_id) {
      flow.scriptVersionId = sv.script_version_id
      flow.scriptText = sv.text ?? ''
    }
  },
  { immediate: true },
)


/* ---------- 解析提交 ---------- */
const { mutate: runParse, isPending: parsing } = useMutation({
  mutationFn: (p: { replace: boolean; range: { episode_start: number; episode_count?: number; episode_end?: number } }) =>
    parseScript(projectId.value, {
      script_version_id: flow.scriptVersionId!,
      text: flow.scriptText,
      ...p.range,
      replace_existing: p.replace || undefined,
    }),
  onSuccess: (res) => {
    flow.parseJobId = res.job_id
    queryClient.invalidateQueries({ queryKey: ['parse-status'] })
  },
  onError: (e) => {
    // 解析前置条件不满足：引导前往评估页完成评估与制作范围确认
    if (errMsg(e).includes('须先评估')) {
      dialog.warning({
        title: '需要先完成评估',
        content: '解析前须先评估剧本并确认制作范围。是否前往评估页面完成评估？',
        positiveText: '前往评估',
        negativeText: '取消',
        onPositiveClick: () => {
          void router.push(`/project/${projectId.value}/estimate`)
        },
      })
      return
    }
    message.error(errMsg(e))
  },
})
const submittedEpsLabel = ref('')
const submitting = ref(false)
/* 解析完成摘要卡片（确认成功后展示） */
const parseDone = ref(false)
/** 基于 store 的 selectedRanges 严格按选中区间解析：区间内过滤已成功集后重新拆子区间，跳过幂等重复提交 */
function handleParse() {
  if (!flow.scriptVersionId || !flow.selectedEpisodes.length || submitting.value) return
  const succeededSet = new Set(episodeGrid.value.filter((e) => e.status === 'succeeded').map((e) => e.episode_no))
  // 每个选中区间内过滤已成功集，再拆分为连续待提交子区间
  const todoRanges: Array<{ start: number; end: number }> = []
  for (const range of flow.selectedRanges) {
    let s = -1
    let prev = -1
    const pushRange = () => { if (s >= 0) todoRanges.push({ start: s, end: prev }) }
    for (let ep = range.start; ep <= range.end; ep++) {
      if (succeededSet.has(ep)) { pushRange(); s = -1; continue }
      if (s < 0) { s = ep; prev = ep; continue }
      if (ep === prev + 1) { prev = ep; continue }
      pushRange()
      s = ep
      prev = ep
    }
    pushRange()
  }
  const todoCount = todoRanges.reduce((n, r) => n + (r.end - r.start + 1), 0)
  if (!todoCount) {
    dialog.info({
      title: '所选集数均已解析完成',
      content: '无需重复解析。公共资产图（角色/场景/道具）请在资产库中生成。',
      positiveText: '去资产库',
      onPositiveClick: () => {
        void router.push(`/project/${projectId.value}/assets`)
      },
    })
    return
  }
  const skipped = flow.selectedEpisodes.length - todoCount
  submitting.value = true
  parseDone.value = false
  const firstEp = todoRanges[0].start
  const lastEp = todoRanges[todoRanges.length - 1].end
  const submittedLabel = todoCount === 1 ? `第 ${firstEp} 集` : `第 ${firstEp}-${lastEp} 集`
  submittedEpsLabel.value = submittedLabel
  for (const r of todoRanges) {
    runParse({ replace: hasResults.value, range: { episode_start: r.start, episode_end: r.end } })
  }
  let msg = '已提交 ' + todoRanges.length + ' 个区间解析任务，共 ' + todoCount + ' 集'
  if (skipped) msg += '（已跳过 ' + skipped + ' 个已完成集）'
  message.success(msg)
  submitting.value = false
}

/* ---------- 解析任务轮询 ---------- */
const { data: parseJobData, isError: parseJobError } = useQuery({
  queryKey: computed(() => ['job', flow.parseJobId]),
  queryFn: () => getJob(flow.parseJobId!),
  enabled: computed(() => !!flow.parseJobId),
  refetchInterval: (query) => {
    const data = query.state.data
    return data && (data.status === 'queued' || data.status === 'running') ? 2000 : false
  },
})
const parseJob = computed(() => parseJobData.value ?? null)
const parseJobRunning = computed(
  () =>
    !!flow.parseJobId &&
    !parseJobError.value &&
    (!parseJob.value || parseJob.value.status === 'queued' || parseJob.value.status === 'running'),
)
const parseElapsed = ref(0)
/** 任务状态查询失败（任务已清理或接口异常）：停止转圈，给用户明确出口 */
const parseJobLost = computed(() => !!flow.parseJobId && parseJobError.value)
/** 排队过久（worker 未消费该任务）：避免用户误以为模型正在思考 */
const queuedTooLong = computed(() => parseJob.value?.status === 'queued' && parseElapsed.value >= 60)
/** 流式占位文案：区分排队中 / 等待过久 / 正常预检中 */
const llmPlaceholder = computed(() => {
  if (queuedTooLong.value) {
    return '任务已排队超过 1 分钟，后端分析引擎可能未运行或繁忙，请检查服务后重试…'
  }
  if (parseJob.value?.status === 'queued') {
    return '任务排队中，等待分析引擎接管…'
  }
  if (parseElapsed.value >= 180) {
    return '已等待超过 3 分钟仍未收到模型输出，模型接口可能超时，建议返回剧本页重新发起分析…'
  }
  return '正在预检与评估剧本质量，分析内容即将开始输出…'
})
let elapsedTimerId: ReturnType<typeof setInterval> | null = null
function ensureElapsedTimer() {
  if (elapsedTimerId !== null) return
  elapsedTimerId = setInterval(() => {
    if (parseJobRunning.value) parseElapsed.value += 1
    else if (elapsedTimerId) { clearInterval(elapsedTimerId); elapsedTimerId = null }
  }, 1000)
}
watch(() => parseJobRunning.value, (running) => { if (running) ensureElapsedTimer() }, { immediate: true })
onUnmounted(() => { if (elapsedTimerId) clearInterval(elapsedTimerId) })
/* ---------- LLM 流式输出（SSE agent.llm_delta 事件） ---------- */
const llmStreamText = ref('')
const llmStreamBoxRef = ref<HTMLElement | null>(null)
let llmEventSource: EventSource | null = null
let llmLastSeq = 0
let llmSubscribedJobId = ''
let llmReconnectTimer: ReturnType<typeof setTimeout> | null = null

function appendLlmDelta(delta: string) {
  llmStreamText.value += delta
  nextTick(() => {
    const el = llmStreamBoxRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function closeLlmStream() {
  if (llmReconnectTimer) {
    clearTimeout(llmReconnectTimer)
    llmReconnectTimer = null
  }
  if (llmEventSource) {
    llmEventSource.close()
    llmEventSource = null
  }
}

async function openLlmStream(jobId: string) {
  // 1. 先回放历史：llm-stream 接口返回已累积文本与最新 seq
  try {
    const res = await fetch(`/api/jobs/${jobId}/llm-stream`)
    if (res.ok) {
      const data = await res.json()
      llmStreamText.value = data.text || ''
      llmLastSeq = data.last_seq || 0
    }
  } catch { /* 回放失败不阻断增量订阅 */ }
  // 2. 从最新 seq 订阅 SSE 增量
  connectLlmSse(jobId)
}

function connectLlmSse(jobId: string) {
  if (llmEventSource || llmSubscribedJobId !== jobId) return
  try {
    llmEventSource = new EventSource('/api/events?last_seq=' + llmLastSeq)
    // 后端 SSE 不带 event: 名称行（统一 message 类型），用 onmessage 接收并按 event_type 过滤
    llmEventSource.onmessage = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data)
        if (data.seq) llmLastSeq = data.seq
        if (data.event_type === 'agent.llm_delta' && data.id === jobId && data.delta) {
          appendLlmDelta(data.delta)
        }
      } catch { /* 忽略非 JSON 消息 */ }
    }
    llmEventSource.onerror = () => {
      // 断线：关闭自动重连，用最新 seq 延迟手动重建，避免服务端重发导致文本重复
      if (llmEventSource) {
        llmEventSource.close()
        llmEventSource = null
      }
      if (llmSubscribedJobId === jobId && !llmReconnectTimer) {
        llmReconnectTimer = setTimeout(() => {
          llmReconnectTimer = null
          if (llmSubscribedJobId === jobId) connectLlmSse(jobId)
        }, 2000)
      }
    }
  } catch {
    llmEventSource = null
  }
}

watch(
  [() => flow.parseJobId, parseJobRunning],
  ([jobId, running]) => {
    // 任务运行中才订阅；任务结束或无任务时关闭连接
    if (jobId && running) {
      if (llmSubscribedJobId === jobId && llmEventSource) return
      closeLlmStream()
      llmSubscribedJobId = jobId
      llmStreamText.value = ''
      llmLastSeq = 0
      openLlmStream(jobId)
    } else {
      llmSubscribedJobId = ''
      closeLlmStream()
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  llmSubscribedJobId = ''
  closeLlmStream()
})

/* ---------- 集级解析状态 ---------- */
const { data: parseStatusData } = useQuery({
  queryKey: computed(() => ['parse-status', projectId.value]),
  queryFn: () => getParseStatus(projectId.value),
  enabled: computed(() => !!projectId.value),
  refetchInterval: computed(() => (parseJobRunning.value ? 2000 : false)),
})
const parseStatus = computed(() => parseStatusData.value ?? null)
const episodeNumbers = computed<number[]>(() => {
  if (flow.selectedEpisodes.length) return [...flow.selectedEpisodes].sort((a, b) => a - b)
  // 兜底：历史数据的 version.total_episodes 可能为空，此时用已有解析记录中的最大集号反推集数，
  // 否则集数会被算成 0，导致集数方格区与完成卡片都显示不出结果
  const records = parseStatus.value?.records ?? []
  const maxRecorded = records.reduce((m, r) => Math.max(m, r.episode_no), 0)
  const total =
    flow.estimate?.estimated_episodes ??
    parseStatus.value?.total_episodes ??
    flow.totalEpisodes ??
    maxRecorded
  return Array.from({ length: total }, (_, i) => i + 1)
})
const statusTotalEpisodes = computed(() => episodeNumbers.value.length)
const episodeGrid = computed(() => {
  const eps = episodeNumbers.value
  const recordMap = new Map<EpisodeParseStatusItem['episode_no'], EpisodeParseStatusItem>(
    (parseStatus.value?.records ?? []).map((r) => [r.episode_no, r]),
  )
  return eps.map((no) => {
    const rec = recordMap.get(no)
    return {
      episode_no: no,
      status: rec?.status ?? 'pending',
      attempt_count: rec?.attempt_count ?? 0,
      error_msg: rec?.error_msg ?? '',
      asset_count: rec?.asset_count ?? 0,
    }
  })
})
const failedEpisodes = computed(() => episodeGrid.value.filter((e) => e.status === 'failed'))
const showFailedOnly = ref(false)
const visibleEpisodes = computed(() =>
  showFailedOnly.value ? episodeGrid.value.filter((e) => e.status === 'failed') : episodeGrid.value,
)
const statusSummary = computed(() => {
  const counts: Record<string, number> = { pending: 0, running: 0, succeeded: 0, failed: 0 }
  for (const e of episodeGrid.value) counts[e.status] = (counts[e.status] ?? 0) + 1
  return counts
})
function statusLabel(s: string) {
  return s === 'succeeded' ? '已完成' : s === 'failed' ? '失败' : s === 'running' ? '进行中' : '待解析'
}
const { mutate: doRetryParse, isPending: retrying } = useMutation({
  mutationFn: () => retryParse(projectId.value),
  onSuccess: (res) => {
    flow.parseJobId = res.job_id
    parseDone.value = false
    queryClient.invalidateQueries({ queryKey: ['parse-status'] })
    message.success(`已重新入队 ${res.retried_episodes.length} 个失败集`)
  },
  onError: (e) => message.error(errMsg(e)),
})
/* ---------- Agent 继续分析：对未解析集触发 script-agent（episode_start 指向首个未完成集） ---------- */
const pendingEpisodes = computed(() => episodeGrid.value.filter((e) => e.status !== 'succeeded'))
const { mutate: doContinueAgent, isPending: continuing } = useMutation({
  mutationFn: () =>
    runScriptAgent(
      projectId.value,
      flow.scriptVersionId!,
      flow.scriptText,
      pendingEpisodes.value[0]?.episode_no,
    ),
  onSuccess: (res) => {
    flow.parseJobId = res.job_id
    parseDone.value = false
    queryClient.invalidateQueries({ queryKey: ['parse-status'] })
    message.success('已继续分析未解析集')
  },
  onError: (e) => message.error(errMsg(e)),
})
const candidateReady = computed(() => parseJob.value?.status === 'succeeded')

/* ---------- 刷新恢复 ---------- */

const candidateResult = computed<ParseJobResult | null>(
  () => (parseJob.value?.result as ParseJobResult | undefined) ?? null,
)
watch(() => candidateResult.value?.parse_version_id, (v) => {
  if (v && !flow.candidateVersionId) flow.candidateVersionId = v
}, { immediate: true })
/* ---------- job.result JSON 转可读格式化文本 ---------- */
function fmtSeverity(s: string) {
  const m: Record<string, string> = { critical: '严重', major: '重要', minor: '轻微' }
  return m[s] ?? s
}
const formattedResult = computed(() => {
  const r = candidateResult.value
  if (!r) return ''
  const lines: string[] = []
  const ev = (r as any).evaluation as Record<string, any> | undefined
  if (ev) {
    lines.push('══ 剧本评估 ══')
    if (ev.overall_score != null) lines.push(`综合评分：${ev.overall_score} / 10`)
    if (ev.summary) lines.push(`总结：${ev.summary}`)
    if (Array.isArray(ev.strengths) && ev.strengths.length) {
      lines.push('')
      lines.push('▸ 优点')
      for (const s of ev.strengths) {
        const detail = typeof s === 'string' ? s : (s.detail || JSON.stringify(s))
        lines.push(`  ✓ ${detail}`)
      }
    }
    if (Array.isArray(ev.issues) && ev.issues.length) {
      lines.push('')
      lines.push('▸ 问题')
      for (const s of ev.issues) {
        const detail = typeof s === 'string' ? s : s.detail || ''
        const sev = typeof s === 'object' && s.severity ? ` [${fmtSeverity(s.severity)}]` : ''
        lines.push(`  ✗${sev} ${detail}`)
      }
    }
    if (Array.isArray(ev.suggestions) && ev.suggestions.length) {
      lines.push('')
      lines.push('▸ 修改建议')
      for (const s of ev.suggestions) {
        const detail = typeof s === 'string' ? s : (s.suggestion || s.detail || JSON.stringify(s))
        lines.push(`  → ${detail}`)
      }
    }
    if (Array.isArray(ev.production_risks) && ev.production_risks.length) {
      lines.push('')
      lines.push('▸ 制作风险')
      for (const s of ev.production_risks) {
        const detail = typeof s === 'string' ? s : s.detail || ''
        const sev = typeof s === 'object' && s.severity ? ` [${fmtSeverity(s.severity)}]` : ''
        lines.push(`  ⚠${sev} ${detail}`)
      }
    }
    if (Array.isArray(ev.plot_holes) && ev.plot_holes.length) {
      lines.push('')
      lines.push('▸ 剧情漏洞')
      for (const s of ev.plot_holes) {
        const detail = typeof s === 'string' ? s : s.detail || ''
        const sev = typeof s === 'object' && s.severity ? ` [${fmtSeverity(s.severity)}]` : ''
        lines.push(`  ⚡${sev} ${detail}`)
      }
    }
    lines.push('')
  }
  // 解析结果摘要
  lines.push('══ 解析结果 ══')
  if (Array.isArray(r.characters) && r.characters.length) {
    lines.push(`角色（${r.characters.length}）：`)
    for (const c of r.characters) lines.push(`  · ${c.name}${c.description ? ' — ' + c.description : ''}`)
    lines.push('')
  }
  if (Array.isArray(r.props) && r.props.length) {
    lines.push(`道具（${r.props.length}）：`)
    for (const p of r.props) lines.push(`  · ${p.name}${p.description ? ' — ' + p.description : ''}`)
    lines.push('')
  }
  if (Array.isArray(r.scenes) && r.scenes.length) {
    const shotCount = r.scenes.reduce((n: number, sc: any) => n + (sc.shots?.length || 0), 0)
    lines.push(`场景（${r.scenes.length}） / 分镜（${shotCount}）：`)
    for (const sc of r.scenes as any[]) {
      const shots = (sc.shots || []) as any[]
      const epSet = new Set(shots.map((s: any) => s.episode).filter(Boolean))
      const epLabel = epSet.size ? `[第 ${[...epSet].sort((a: any,b: any) => a-b).join('、')} 集]` : ''
      lines.push(`  ▸ ${sc.title || '未命名'}${sc.time_note ? '（' + sc.time_note + '）' : ''} ${epLabel}  ${shots.length} 镜`)
      for (const sh of shots) {
        const prefix = sh.shot_no ? `#${sh.shot_no}` : ''
        lines.push(`     ${prefix} ${sh.description || ''}`.trimEnd())
      }
    }
    lines.push('')
  }
  const warns = (r as any).parse_warnings as any[] | undefined
  if (warns?.length) {
    lines.push(`解析警告（${warns.length} 条）：`)
    for (const w of warns) lines.push(`  ⚠ ${typeof w === 'string' ? w : w.message || JSON.stringify(w)}`)
  }
  return lines.join('\n')
})

/** 刷新恢复：项目已有已确认解析版本且全部集解析成功时，恢复完成态与『去资产库』引导 */
const allEpisodesSucceeded = computed(
  () => statusTotalEpisodes.value > 0 && statusSummary.value.succeeded === statusTotalEpisodes.value,
)
watch(
  () => [project.value?.current_parse_version_id, allEpisodesSucceeded.value, parseJobRunning.value] as const,
  ([versionId, allDone, running]) => {
    if (versionId && allDone && !running) parseDone.value = true
  },
  { immediate: true },
)

/* ---------- 确认候选版本 ---------- */
const { mutate: doConfirmParse } = useMutation({
  mutationFn: () => confirmParseVersion(projectId.value, flow.candidateVersionId!, project.value?.current_parse_version_id),
  onSuccess: () => {
    message.success('解析结果已确认，场景/角色/道具已更新')
    parseDone.value = true
    flow.resetAfterConfirmParse()
    void queryClient.invalidateQueries({ queryKey: ['scenes', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['characters', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e) => {
    // 幂等处理：多批次重复确认时版本已是 current（仅候选状态可确认），视为已确认成功，不弹错误
    const msg = errMsg(e)
    if (msg.includes('仅候选状态') || msg.includes('conflict')) {
      parseDone.value = true
      flow.resetAfterConfirmParse()
      return
    }
    message.error(msg)
  },
})
/** Agent 分批解析：首批完成后 has_more=true，剩余集需用户『继续分析』推进，此时不自动确认 */
const agentHasMore = computed(
  () => (parseJob.value?.result as { has_more?: boolean } | undefined)?.has_more === true,
)
watch(
  () => [candidateReady.value, flow.candidateVersionId, agentHasMore.value] as const,
  ([ready, vid, more]) => {
    if (ready && vid && !more) doConfirmParse()
  },
  { immediate: true },
)

/* ---------- 场景/角色/道具 ---------- */
const { data: scenesData } = useQuery({
  queryKey: computed(() => ['scenes', projectId.value]),
  queryFn: () => listScenes(projectId.value, { page_size: 100 }),
  enabled: computed(() => !!projectId.value),
})
const scenes = computed<SceneWithShots[]>(
  () => (scenesData.value?.items as SceneWithShots[] | undefined) ?? [],
)
const hasResults = computed(() => scenes.value.length > 0)
const { data: charactersData } = useQuery({
  queryKey: computed(() => ['characters', projectId.value]),
  queryFn: () => listCharacters(projectId.value, { page_size: 100 }),
  enabled: computed(() => !!projectId.value),
})
const characters = computed(() => charactersData.value?.items ?? [])
const { data: propsData } = useQuery({
  queryKey: computed(() => ['props', projectId.value]),
  queryFn: () => listProps(projectId.value!),
  enabled: computed(() => !!projectId.value),
})
const props = computed(() => propsData.value?.items ?? [])
</script>

<template>
  <section class="parse-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">解析</h1>
        <FlowNav :project-id="projectId" />
        <span class="project-name">{{ project?.name ?? '' }}</span>
      </div>
      <div class="header-actions">
        <NButton quaternary @click="router.push(`/project/${projectId}/script`)">返回剧本</NButton>
        <NButton
          v-if="pendingEpisodes.length && !parseJobRunning"
          type="primary"
          :loading="continuing"
          @click="doContinueAgent()"
        >
          继续分析（{{ pendingEpisodes.length }} 集未完成）
        </NButton>
        <NButton
          v-if="flow.selectedEpisodes.length"
          :type="statusTotalEpisodes && flow.selectedEpisodes.length && !flow.candidateVersionId ? 'primary' : 'default'"
          :disabled="!flow.scriptVersionId || !statusTotalEpisodes || !flow.selectedEpisodes.length || !!flow.candidateVersionId || submitting"
          :loading="parsing"
          @click="handleParse()"
        >
          解析选中（{{ flow.selectedEpisodes.length }} 集）
        </NButton>
      </div>
    </header>
    <div class="page-body">
      <div class="content-pane">
        <!-- LLM 流式输出区：分析进行中实时展示模型返回内容（评估阶段显示占位提示） -->
        <div v-if="flow.parseJobId && parseJobRunning && !parseJobLost" class="llm-stream-card">
          <div class="llm-stream-head">
            <i class="spin-dot" />
            <span>AI 分析输出中…</span>
          </div>
          <div ref="llmStreamBoxRef" class="llm-stream-body">{{ llmStreamText || llmPlaceholder }}</div>
        </div>
        <!-- 候选状态窄条 -->
        <div v-if="flow.parseJobId" class="candidate-bar" :class="`is-${parseJob?.status ?? 'running'}`">
          <template v-if="parseJobRunning">
            <i class="spin-dot" />
            <span class="candidate-bar-text">正在解析 {{ submittedEpsLabel || (statusTotalEpisodes ? `全部 ${statusTotalEpisodes} 集` : '剧本') }}…已耗时 {{ parseElapsed }}s</span>
          </template>
          <template v-else-if="candidateReady && agentHasMore">
            <span class="candidate-bar-badge">✓</span>
            <span class="candidate-bar-text">已解析 {{ statusSummary.succeeded }}/{{ statusTotalEpisodes }} 集，还有 {{ pendingEpisodes.length }} 集待继续</span>
            <NButton size="small" type="primary" :loading="continuing" @click="doContinueAgent()">继续分析</NButton>
          </template>
          <template v-else-if="candidateReady">
            <span class="candidate-bar-badge">✓</span>
            <span class="candidate-bar-text">解析完成，正在应用结果…</span>
          </template>
          <template v-else-if="parseJobLost">
            <span class="candidate-bar-badge is-error">!</span>
            <span class="candidate-bar-text">无法获取分析任务状态（任务可能已过期），请返回剧本页重新发起分析</span>
            <NButton size="small" quaternary @click="router.push(`/project/${projectId}/script`)">返回剧本</NButton>
          </template>
          <template v-else>
            <span class="candidate-bar-badge is-error">!</span>
            <span class="candidate-bar-text">解析失败：{{ parseJob?.error_msg || '未知原因' }}</span>
            <NButton size="small" @click="handleParse()">重新解析</NButton>
          </template>
        </div>

        <!-- 解析结果可读文本面板 -->
        <div v-if="candidateReady && formattedResult" class="formatted-result-card">
          <div class="formatted-result-head">
            <span>📋 解析报告</span>
          </div>
          <pre class="formatted-result-body">{{ formattedResult }}</pre>
        </div>
        <!-- 解析完成摘要卡片 -->
        <div v-if="parseDone" class="parse-done-card">
          <div class="parse-done-head">
            <span class="parse-done-badge"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 8.5l3.2 3.2L13 5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" /></svg></span>
            <span class="parse-done-title">解析完成</span>
          </div>
          <div class="parse-done-stats">
            <div class="parse-done-stat">
              <span class="parse-done-stat-value">{{ statusSummary.succeeded || statusTotalEpisodes }}</span>
              <span class="parse-done-stat-label">集</span>
            </div>
            <div class="parse-done-stat">
              <span class="parse-done-stat-value">{{ scenes.length }}</span>
              <span class="parse-done-stat-label">场景</span>
            </div>
            <div class="parse-done-stat">
              <span class="parse-done-stat-value">{{ characters.length }}</span>
              <span class="parse-done-stat-label">角色</span>
            </div>
            <div class="parse-done-stat">
              <span class="parse-done-stat-value">{{ props.length }}</span>
              <span class="parse-done-stat-label">道具</span>
            </div>
          </div>
          <div class="parse-done-actions">
            <NButton type="primary" @click="router.push(`/project/${projectId}/assets`)">去资产库 →</NButton>
          </div>
        </div>

        <!-- 集数解析状态：分析进行中不展示，分析完成后才显示 -->
        <div v-if="statusTotalEpisodes && !parseJobRunning" class="parse-status-panel panel-card">
          <div class="episode-toolbar">
            <span class="pane-label">集数解析状态</span>
            <span class="episode-count-tag">{{ statusSummary.succeeded }}/{{ statusTotalEpisodes || 0 }} 集完成</span>
            <span v-if="flow.selectedEpisodes.length" class="episode-count-tag" style="background:rgba(99,102,241,0.08);color:var(--brand-primary,#6366f1);">选中 {{ flow.selectedEpisodes.length }} 集</span>
            <button
              class="quick-chip"
              :class="{ active: showFailedOnly }"
              :disabled="!failedEpisodes.length"
              @click="showFailedOnly = !showFailedOnly"
            >
              只看失败（{{ statusSummary.failed }}）
            </button>
            <NButton
              v-if="failedEpisodes.length"
              size="small"
              type="warning"
              :loading="retrying"
              @click="doRetryParse()"
            >
              重试失败集（{{ failedEpisodes.length }}）
            </NButton>
          </div>
          <div class="episode-grid">
            <div
              v-for="ep in visibleEpisodes"
              :key="ep.episode_no"
              class="episode-cell"
              :class="`is-${ep.status}`"
              :title="`第 ${ep.episode_no} 集：${statusLabel(ep.status)}${ep.asset_count ? '，解析出 ' + ep.asset_count + ' 个分镜' : ''}${ep.error_msg ? '，' + ep.error_msg : ''}`"
            >
              <span class="episode-cell-no">第 {{ ep.episode_no }} 集</span>
              <span class="episode-cell-meta" :class="ep.status !== 'succeeded' ? ['status-tag', `is-${ep.status}`] : undefined">{{ ep.status === 'succeeded' ? ep.asset_count + ' 个分镜' : statusLabel(ep.status) }}</span>
              <button
                v-if="ep.status === 'failed'"
                class="episode-cell-retry"
                :disabled="retrying"
                @click.stop="doRetryParse()"
                title="重试失败集"
              >↻</button>
            </div>
            <div v-if="failedEpisodes.length && showFailedOnly" class="failed-error-list">
              <div v-for="ep in failedEpisodes" :key="`err-${ep.episode_no}`" class="failed-error-card">
                <div class="failed-error-head">
                  <span class="failed-error-ep">第 {{ ep.episode_no }} 集</span>
                  <button class="failed-error-retry" :disabled="retrying" @click="doRetryParse()">重试</button>
                </div>
                <p v-if="ep.error_msg" class="failed-error-msg">{{ ep.error_msg }}</p>
                <p v-else class="failed-error-msg muted">解析失败，请重试</p>
              </div>
            </div>
          </div>
          <div class="episode-legend">
            <span class="legend-item"><i class="dot is-pending" />待解析 {{ statusSummary.pending }}</span>
            <span class="legend-item"><i class="dot is-running" />进行中 {{ statusSummary.running }}</span>
            <span class="legend-item"><i class="dot is-succeeded" />已完成 {{ statusSummary.succeeded }}</span>
            <span class="legend-item"><i class="dot is-failed" />失败 {{ statusSummary.failed }}</span>
          </div>
        </div>

        <!-- 音频分析结果（音频模块改造：解析完成后可从解析结果生成音频规划） -->
        <AudioPlanPanel
          v-if="projectId && !parseJobRunning && (parseDone || statusSummary.succeeded > 0)"
          :project-id="projectId"
        />
        <NEmpty v-if="flow.parseJobId || parseDone" description="" />
        <NEmpty v-else-if="flow.selectedEpisodes.length" description="请点击『解析选中』提交任务" />
        <NEmpty v-else description="Agent 将自动完成评估与解析，请稍候" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.formatted-result-card { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 12px; background: var(--bg-panel, #fff); margin-bottom: 12px; overflow: hidden; }
.formatted-result-head { padding: 12px 16px; font-size: 14px; font-weight: 600; color: var(--text-primary, #111); border-bottom: 1px solid var(--border-subtle, #e5e7eb); background: rgba(99, 102, 241, 0.04); }
.formatted-result-body { margin: 0; padding: 16px 20px; font-size: 13px; line-height: 1.8; color: var(--text-primary, #333); white-space: pre-wrap; word-break: break-word; max-height: 600px; overflow-y: auto; font-family: inherit; }
.parse-done-card { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; border: 1px solid rgba(34, 197, 94, 0.35); border-radius: 14px; padding: 20px 28px; background: rgba(34, 197, 94, 0.06); }
.parse-done-head { display: flex; align-items: center; gap: 8px; }
.parse-done-badge { width: 26px; height: 26px; border-radius: 50%; background: var(--state-success, #22c55e); color: #fff; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.parse-done-badge svg { display: block; }
.parse-done-title { font-size: 22px; font-weight: 700; letter-spacing: 0.3px; color: var(--text-primary, #333); }
.parse-done-sub { font-size: 12px; color: var(--text-secondary, #64748b); }
.parse-done-stats { display: flex; gap: 32px; }
.parse-done-stat { display: flex; align-items: baseline; gap: 6px; }
.parse-done-stat-value { font-size: 28px; font-weight: 700; color: var(--text-primary, #e5e7eb); }
.parse-done-stat-label { font-size: 14px; color: var(--text-secondary, #64748b); }
.parse-done-actions { display: flex; gap: 10px; margin-left: auto; }
.parse-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle, #e5e7eb);
  flex-shrink: 0;
}
.header-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.page-title { font-size: 16px; font-weight: 600; margin: 0; }
.project-name { font-size: 12px; color: var(--text-secondary, #64748b); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.page-body { display: flex; flex: 1; min-height: 0; }
.content-pane { flex: 1; padding: 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; }
.candidate-bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 8px; font-size: 13px; }
.llm-stream-card { margin-bottom: 10px; border: 1px solid rgba(99, 226, 183, 0.35); border-radius: 8px; overflow: hidden; background: rgba(16, 24, 40, 0.6); }
.llm-stream-head { display: flex; align-items: center; gap: 8px; padding: 6px 12px; font-size: 12px; color: #63e2b7; background: rgba(99, 226, 183, 0.08); }
.llm-stream-body { max-height: 360px; overflow-y: auto; padding: 14px 16px; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 14px; line-height: 1.7; white-space: pre-wrap; word-break: break-all; color: rgba(224, 232, 240, 0.9); }
.candidate-bar.is-running { background: rgba(99, 102, 241, 0.06); }
.candidate-bar.is-succeeded { background: rgba(34, 197, 94, 0.08); }
.candidate-bar.is-failed { background: rgba(208, 48, 80, 0.06); }
.candidate-bar-badge { width: 18px; height: 18px; border-radius: 50%; background: var(--brand-primary, #6366f1); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 11px; }
.candidate-bar-badge.is-error { background: var(--brand-error, #d03050); }
.candidate-bar-text { flex: 1; }
.spin-dot { width: 12px; height: 12px; border: 2px solid var(--brand-primary, #6366f1); border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.parse-status-panel { border: 1px solid var(--border-subtle, #e0e0e6); border-radius: 10px; padding: 18px; display: flex; flex-direction: column; gap: 14px; }
.episode-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pane-label { font-size: 16px; font-weight: 600; color: var(--text-primary, #333); }
.episode-count-tag { font-size: 14px; font-weight: 600; color: var(--brand-primary, #6366f1); font-variant-numeric: tabular-nums; }
.quick-chip { border: 1px solid var(--border-subtle, #e0e0e6); background: none; border-radius: 999px; padding: 3px 12px; font-size: 12px; color: var(--text-secondary); cursor: pointer; transition: all 0.15s ease; }
.quick-chip:hover:not(:disabled) { border-color: var(--brand-primary, #6366f1); color: var(--brand-primary, #6366f1); }
.quick-chip.active { background: rgba(99, 102, 241, 0.1); border-color: var(--brand-primary, #6366f1); color: var(--brand-primary, #6366f1); }
.quick-chip:disabled { opacity: 0.4; cursor: not-allowed; }
.episode-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
.episode-cell { position: relative; min-height: 84px; padding: 14px 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; border-radius: 10px; border: 1px solid transparent; transition: transform 0.15s ease; }
.episode-cell:hover { transform: translateY(-1px); }
.episode-cell-no { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.2; white-space: nowrap; }
.episode-cell-meta { font-size: 14px; line-height: 1.3; opacity: 0.95; white-space: nowrap; }
@keyframes cell-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(99, 102, 241, 0.3); } 50% { box-shadow: 0 0 0 4px rgba(99, 102, 241, 0); } }
.episode-cell.is-running { animation: cell-pulse 1.6s ease-in-out infinite; }
.episode-cell.is-pending { background: rgba(128, 128, 128, 0.12); color: var(--text-secondary, #888); }
.episode-cell.is-running { background: rgba(99, 102, 241, 0.15); color: var(--brand-primary, #6366f1); border-color: rgba(99, 102, 241, 0.4); }
.episode-cell.is-succeeded { background: rgba(34, 197, 94, 0.1); color: var(--state-success, #22c55e); border-color: rgba(34, 197, 94, 0.25); }
.episode-cell.is-failed { background: rgba(208, 48, 80, 0.12); color: var(--brand-error, #d03050); border-color: rgba(208, 48, 80, 0.4); }
.episode-cell-retry { position: absolute; top: 3px; right: 3px; width: 20px; height: 20px; border: none; border-radius: 50%; background: rgba(208, 48, 80, 0.15); color: var(--brand-error, #d03050); font-size: 12px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background 0.15s ease; }
.episode-cell-retry:hover:not(:disabled) { background: rgba(208, 48, 80, 0.3); }
.episode-cell-retry:disabled { opacity: 0.4; cursor: not-allowed; }
.failed-error-list { grid-column: 1 / -1; display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.failed-error-card { border: 1px solid rgba(208, 48, 80, 0.25); border-radius: 8px; padding: 8px 10px; background: rgba(208, 48, 80, 0.04); }
.failed-error-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
.failed-error-ep { font-size: 13px; font-weight: 600; color: var(--brand-error, #d03050); }
.failed-error-retry { border: 1px solid rgba(208, 48, 80, 0.3); background: none; border-radius: 4px; padding: 3px 12px; font-size: 12px; color: var(--brand-error, #d03050); cursor: pointer; transition: all 0.15s ease; }
.failed-error-retry:hover:not(:disabled) { background: rgba(208, 48, 80, 0.1); }
.failed-error-retry:disabled { opacity: 0.4; cursor: not-allowed; }
.failed-error-msg { font-size: 13px; color: var(--text-secondary, #888); line-height: 1.5; margin: 0; white-space: pre-wrap; word-break: break-word; overflow-wrap: anywhere; }
.failed-error-msg.muted { font-style: italic; }
.episode-legend { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; font-size: 14px; color: var(--text-secondary, #888); }
.legend-item { display: inline-flex; align-items: center; gap: 4px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.is-pending { background: rgba(128, 128, 128, 0.4); }
.dot.is-running { background: var(--brand-primary, #6366f1); }
.dot.is-succeeded { background: rgba(34, 197, 94, 0.6); }
.dot.is-failed { background: var(--brand-error, #d03050); }
@media (max-width: 768px) {
  .episode-grid { grid-template-columns: repeat(auto-fill, minmax(88px, 1fr)); }
  .page-header { flex-wrap: wrap; }
  .header-actions { flex-wrap: wrap; }
  .candidate-bar { flex-wrap: wrap; }
  .candidate-bar-text { min-width: 0; word-break: break-word; }
  .parse-done-stats { flex-wrap: wrap; gap: 16px; }
  .parse-done-actions { margin-left: 0; width: 100%; }
}
</style>
