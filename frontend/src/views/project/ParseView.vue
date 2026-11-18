<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, NEmpty, NIcon, NModal, useMessage } from 'naive-ui'
import { SparklesOutline } from '@vicons/ionicons5'
import {
  confirmParseVersion,
  getCurrentScript,
  getParseStatus,
  getProject,
  getScriptAgentStatus,
  parseScript,
  retryParse,
  runScriptAgent,
} from '@/api/projects'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { getJob, listJobs } from '@/api/jobs'
import type { Job } from '@/types/job'
import type { EpisodeParseStatusItem, Scene, Shot } from '@/types/script'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import FlowNav from '@/components/FlowNav.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const route = useRoute()
const router = useRouter()
const confirmDialog = useConfirmDialog()
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
    // 解析前置条件不满足：评估页已下线，改为引导回剧本页重新发起一体化分析
    if (errMsg(e).includes('须先评估')) {
      confirmDialog({
        title: '需要先完成剧本分析',
        message: '解析前须先完成剧本评估与制作范围确认。请返回剧本页重新发起分析，由 Agent 自动完成评估与解析。',
        confirmText: '返回剧本',
        danger: false,
        onConfirm: () => {
          void router.push(`/project/${projectId.value}/script`)
        },
      })
      return
    }
    message.error(errMsg(e))
  },
})
const submittedEpsLabel = ref('')
const submitting = ref(false)
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
    confirmDialog({
      title: '所选集数均已解析完成',
      message: '无需重复解析。公共资产图（角色/场景/道具）请在资产库中生成。',
      confirmText: '去资产库',
      danger: false,
      onConfirm: () => {
        void router.push(`/project/${projectId.value}/assets`)
      },
    })
    return
  }
  const skipped = flow.selectedEpisodes.length - todoCount
  submitting.value = true
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
/**
 * 耗时以任务真实时间戳为准，不再本地自增：
 * - 起点 priority：started_at（任务真正开始执行）> created_at（排队也计入，但至少不会归零）
 * - 终点：finished_at 存在即冻结，否则取当前时间
 * 这样刷新页面、重新渲染都不会重置计时，任务结束后也不会继续跳。
 */
const nowTick = ref(Date.now())
const analysisStartedMs = computed(() => {
  const j = parseJob.value
  const raw = j?.started_at || j?.created_at
  const ms = raw ? new Date(raw).getTime() : 0
  return Number.isNaN(ms) ? 0 : ms
})
const parseElapsed = computed(() => {
  const start = analysisStartedMs.value
  if (!start) return 0
  const fin = parseJob.value?.finished_at
  const end = fin ? new Date(fin).getTime() : nowTick.value
  if (Number.isNaN(end)) return 0
  return Math.max(0, Math.floor((end - start) / 1000))
})
/** HH:mm:ss（超过 1 小时）或 mm:ss，需求指定格式 */
const elapsedText = computed(() => {
  const s = parseElapsed.value
  const p2 = (n: number) => String(n).padStart(2, '0')
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return h > 0 ? `${p2(h)}:${p2(m)}:${p2(s % 60)}` : `${p2(m)}:${p2(s % 60)}`
})
/** 任务状态查询失败（任务已清理或接口异常）：停止转圈，给用户明确出口 */
const parseJobLost = computed(() => !!flow.parseJobId && parseJobError.value)
/** 排队过久（worker 未消费该任务）：避免用户误以为模型正在思考 */
const queuedTooLong = computed(() => parseJob.value?.status === 'queued' && parseElapsed.value >= 60)
/**
 * 输出区占位文案：只在「已确定在等待、但还没有真实 token」时使用。
 * 原则：只陈述可观测事实（排队/已等待多久），不假装知道后端内部阶段——
 * 后端并不上报「预检中/评估中」这类阶段，写出来就是编造执行过程。
 */
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
  return '已连接分析任务，正在等待模型返回第一段输出…'
})
let elapsedTimerId: ReturnType<typeof setInterval> | null = null
function ensureElapsedTimer() {
  if (elapsedTimerId !== null) return
  elapsedTimerId = setInterval(() => {
    if (parseJobRunning.value) nowTick.value = Date.now()
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
  // 注意：不要在这里回退到 flow.totalEpisodes —— 它是 `estimate?.estimated_episodes ?? 0`，
  // 刷新后 estimate 为 null 时恒为 0，会把整条兜底链截断，导致集数面板与失败/重试入口整体消失。
  const total =
    flow.estimate?.estimated_episodes ??
    parseStatus.value?.total_episodes ??
    maxRecorded
  return Array.from({ length: total }, (_, i) => i + 1)
})
const statusTotalEpisodes = computed(() => episodeNumbers.value.length)
/*
 * 评估页已下线，页面不再提供选集入口：集数确定后若未选集则默认全选，
 * 否则『解析选中』按钮将因 selectedEpisodes 为空而永久不显示。
 */
watch(
  () => statusTotalEpisodes.value,
  (total) => {
    if (total > 0 && !flow.selectedEpisodes.length) flow.selectAllEpisodes(total)
  },
  { immediate: true },
)
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
const formattedResult = computed(() => formatResult(candidateResult.value))
/**
 * job.result → 可读全文。
 * 抽成普通函数，供「当前任务结果」与「从后端补回的历史结果」共用。
 */
function formatResult(r: Record<string, any> | null | undefined): string {
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
}

/**
 * 从后端补回「最近一次成功解析」的 result。
 * 为什么需要：确认解析后 flow.resetAfterConfirmParse() 会清掉 parseJobId，
 * 分析窗（含原文）随之消失，刷新后也没有原文可查。这里独立取一次，只读不写 store。
 */
const { data: archivedParseJobs } = useQuery({
  queryKey: computed(() => ['jobs', 'parse-archive', projectId.value]),
  queryFn: () => listJobs({ project_id: projectId.value, job_type: 'parse', page: 1, page_size: 50 }),
  enabled: computed(() => !!projectId.value),
})
const archivedParseText = computed(() => {
  const items = archivedParseJobs.value?.items ?? []
  const hit = items.find((j: Job) => j.status === 'succeeded' && j.result)
  return formatResult((hit?.result ?? null) as Record<string, any> | null)
})
/** 原文全文：优先当前任务结果，其次后端历史结果 */
const rawReportText = computed(() => formattedResult.value || archivedParseText.value)

/* ---------- 分镜 / 对白分组：直接取自解析结果 scenes[].shots[] ----------
 * 数据边界：解析结果是唯一带 shots 明细的来源（当前任务 result 或历史成功任务 result）。
 * 不额外请求 GET /projects/{id}/shots —— 该接口必须传 episode，多集需 N 次请求，
 * 而解析页只需要「解析出的结构」，完整分镜编辑仍由分镜图/分镜看板负责。
 */
/** 历史解析结果对象（与 archivedParseText 同一查询，只读，不写 store） */
const archivedParseResult = computed<Record<string, any> | null>(() => {
  const items = archivedParseJobs.value?.items ?? []
  const hit = items.find((j: Job) => j.status === 'succeeded' && j.result)
  return (hit?.result ?? null) as Record<string, any> | null
})
/** 展示用解析结果：当前任务优先，其次后端历史 */
const effectiveParseResult = computed<Record<string, any> | null>(
  () => (candidateResult.value as Record<string, any> | null) ?? archivedParseResult.value,
)

type ResultGroupKey = 'scene' | 'character' | 'prop' | 'shot' | 'dialogue' | 'audio'
const activeGroup = ref<ResultGroupKey>('scene')

interface ParseShotRow {
  ep: number
  sceneSeq: number
  sceneTitle: string
  shotNo: number | string
  shotType: string
  duration: number | null
  desc: string
  props: string[]
  who: string
  /** shot_kind：dialogue/narration/visual/transition，用于区分旁白与角色台词 */
  kind: string
  dialogue: Array<{ speaker: string; text: string; emotion: string }>
}

const parseShots = computed<ParseShotRow[]>(() => {
  const r = effectiveParseResult.value
  const out: ParseShotRow[] = []
  for (const sc of (r?.scenes as any[] | undefined) ?? []) {
    const sceneSeq = Number(sc?.seq ?? 0)
    const sceneTitle = String(sc?.title ?? '未命名场景')
    for (const sh of (sc?.shots as any[] | undefined) ?? []) {
      const segs = Array.isArray(sh?.dialogue_segments) ? sh.dialogue_segments : []
      const dlg = segs.length
        ? segs
            .map((g: any) => ({
              speaker: String(g?.speaker ?? ''),
              text: String(g?.text ?? ''),
              emotion: String(g?.emotion ?? ''),
            }))
            .filter((g: { text: string }) => g.text)
        : sh?.dialogue
          ? [{ speaker: String(sh?.speaker ?? ''), text: String(sh.dialogue), emotion: String(sh?.emotion ?? '') }]
          : []
      out.push({
        ep: Number(sh?.episode ?? 0),
        sceneSeq,
        sceneTitle,
        shotNo: (sh?.shot_no ?? sh?.seq ?? '') as number | string,
        shotType: String(sh?.shot_type ?? ''),
        duration: sh?.duration == null ? null : Number(sh.duration),
        desc: String(sh?.description ?? ''),
        props: Array.isArray(sh?.props) ? sh.props.map(String) : [],
        who: String(sh?.character_id ?? sh?.speaker ?? ''),
        kind: String(sh?.shot_kind ?? ''),
        dialogue: dlg,
      })
    }
  }
  const num = (v: unknown) => (typeof v === 'number' ? v : Number.parseInt(String(v), 10) || 0)
  return out.sort((a, b) => a.ep - b.ep || a.sceneSeq - b.sceneSeq || num(a.shotNo) - num(b.shotNo))
})

interface DialogueRow {
  ep: number
  sceneTitle: string
  shotNo: number | string
  speaker: string
  text: string
  emotion: string
  /** 无 speaker 且 shot_kind=narration 时为旁白 */
  narration: boolean
}
const dialogueRows = computed<DialogueRow[]>(() => {
  const rows: DialogueRow[] = []
  for (const s of parseShots.value) {
    for (const d of s.dialogue) {
      rows.push({
        ep: s.ep,
        sceneTitle: s.sceneTitle,
        shotNo: s.shotNo,
        speaker: d.speaker,
        text: d.text,
        emotion: d.emotion,
        narration: !d.speaker && s.kind === 'narration',
      })
    }
  }
  return rows
})
const emotionMap: Record<string, string> = {
  neutral: '平静', happy: '愉快', sad: '悲伤', angry: '愤怒', fearful: '恐惧',
}
const emotionLabel = (e: string) => emotionMap[e] ?? e

/** 分组目录：数量为真实解析条数；音频规划在解析结果中没有对应字段，故不给数字 */
const resultGroups = computed<
  Array<{ key: ResultGroupKey; label: string; count: number | null }>
>(() => [
  { key: 'scene', label: '场景', count: scenes.value.length },
  { key: 'character', label: '角色', count: characters.value.length },
  { key: 'prop', label: '道具', count: props.value.length },
  { key: 'shot', label: '分镜', count: parseShots.value.length },
  { key: 'dialogue', label: '台词', count: dialogueRows.value.length },
  { key: 'audio', label: '音频规划', count: null },
])
const activeGroupLabel = computed(
  () => resultGroups.value.find((g) => g.key === activeGroup.value)?.label ?? '',
)
/** 「分镜明细取不到但库里确有分镜」时的兜底说明用 */
const shotsWithoutDetail = computed(
  () => parseShots.value.length === 0 && totalShots.value > 0,
)

/* ---------- 确认候选版本 ---------- */
const { mutate: doConfirmParse } = useMutation({
  mutationFn: () => confirmParseVersion(projectId.value, flow.candidateVersionId!, project.value?.current_parse_version_id),
  onSuccess: () => {
    message.success('解析结果已确认，场景/角色/道具已更新')
    flow.resetAfterConfirmParse()
    void queryClient.invalidateQueries({ queryKey: ['scenes', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['characters', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e) => {
    // 幂等处理：多批次重复确认时版本已是 current（仅候选状态可确认），视为已确认成功，不弹错误
    const msg = errMsg(e)
    if (msg.includes('仅候选状态') || msg.includes('conflict')) {
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
/** 解析出的镜头总数：场景接口直接带 shot_count */
const totalShots = computed(() => scenes.value.reduce((n, sc) => n + (sc.shot_count ?? 0), 0))
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

/* ---------- 解析结果状态：单一事实来源 ---------- */
/** 有失败集且当前没有任务在跑（含刷新后 parseJobId 已丢失的情况）：失败必须常驻可见 */
const showFailurePanel = computed(() => failedEpisodes.value.length > 0 && !parseJobRunning.value)
/** 解析真正完成：全部集成功。以集级解析状态为唯一事实来源 */
const parseAllSucceeded = computed(
  () => statusTotalEpisodes.value > 0 && statusSummary.value.succeeded === statusTotalEpisodes.value,
)
/** 结果区状态文案与色调：只在这里表达一次解析状态 */
const resultState = computed<{ label: string; tone: string }>(() => {
  if (parseJobRunning.value) return { label: '解析中', tone: 'running' }
  if (showFailurePanel.value) {
    return statusSummary.value.succeeded > 0
      ? { label: '部分成功', tone: 'warning' }
      : { label: '解析失败', tone: 'failed' }
  }
  if (parseAllSucceeded.value) return { label: '解析完成', tone: 'succeeded' }
  if (statusTotalEpisodes.value > 0) return { label: '待解析', tone: 'pending' }
  return { label: '暂无结果', tone: 'pending' }
})
/** 概览统计：全部来自真实解析结果，pendingIssueCount 只统计真实失败集
 *  group 存在即可点击切到对应分组；「集」不是结果分组（任务状态在上方面板），故不给 group
 */
const overviewStats = computed(() => [
  { key: 'episode', label: '集', value: statusTotalEpisodes.value, group: null },
  { key: 'scene', label: '场景', value: scenes.value.length, group: 'scene' as const },
  { key: 'character', label: '角色', value: characters.value.length, group: 'character' as const },
  { key: 'prop', label: '道具', value: props.value.length, group: 'prop' as const },
  { key: 'shot', label: '分镜', value: totalShots.value, group: 'shot' as const },
  { key: 'dialogue', label: '台词', value: dialogueRows.value.length, group: 'dialogue' as const },
])
const pendingIssueCount = computed(() => failedEpisodes.value.length)

/* ---------- 刷新恢复：从后端最近一次 script_agent 任务恢复运行态 ----------
 * 依赖 GET /projects/{id}/script-agent/status（返回最近一个 script_agent 任务的 id/status）。
 * 只恢复「仍在排队/运行」的任务：已结束的任务不接管，避免页面重新进入失败态展示，
 * 失败信息由集级解析状态 + 失败面板负责（单一事实来源）。
 */
const { data: agentStatusData } = useQuery({
  queryKey: computed(() => ['script-agent-status', projectId.value]),
  queryFn: () => getScriptAgentStatus(projectId.value),
  enabled: computed(() => !!projectId.value),
  refetchInterval: computed(() => (flow.parseJobId ? false : 5000)),
})
watch(
  () => agentStatusData.value,
  (s) => {
    if (!s?.job || flow.parseJobId) return
    // 活跃任务必须接管（刷新后继续显示实时输出与耗时）；
    // 失败任务也要接管，否则刷新后看不到「执行失败 + 耗时」；
    // 已成功不接管——成功后按需求收起执行过程，只展示解析结果。
    if (['queued', 'running', 'failed', 'lost'].includes(s.job.status)) {
      flow.parseJobId = s.job.id
    }
  },
  { immediate: true },
)

/* ---------- 统一分析窗：状态 / 阶段 / 底部标签 ---------- */
/** 何时展示分析窗：有任务在跟踪，或项目尚未解析完成（此时需要告诉用户「等待执行」） */
const analysisVisible = computed(
  () =>
    !!flow.parseJobId ||
    statusTotalEpisodes.value === 0 ||
    statusSummary.value.pending === statusTotalEpisodes.value,
)
const analysisState = computed<{ label: string; tone: string }>(() => {
  if (parseJobLost.value) return { label: '状态未知', tone: 'failed' }
  if (!flow.parseJobId) return { label: '等待执行', tone: 'idle' }
  if (parseJobRunning.value) return { label: '正在分析', tone: 'running' }
  const st = parseJob.value?.status
  if (st === 'succeeded') return { label: '已完成', tone: 'succeeded' }
  if (st === 'failed' || st === 'lost') return { label: '执行失败', tone: 'failed' }
  if (st === 'cancelled') return { label: '已取消', tone: 'idle' }
  return { label: '等待执行', tone: 'idle' }
})
const analysisStage = computed(() => {
  if (!flow.parseJobId) return '尚未提交分析，返回剧本页点击「开始分析」'
  if (parseJobLost.value) return '无法获取任务状态（任务可能已过期）'
  const st = parseJob.value?.status
  if (st === 'queued') return '任务排队中，等待分析引擎接管'
  if (parseJobRunning.value) {
    return llmStreamText.value ? 'AI 正在输出分析内容' : llmPlaceholder.value
  }
  if (st === 'succeeded') return `已解析 ${statusSummary.value.succeeded}/${statusTotalEpisodes.value} 集`
  if (st === 'failed') return '分析中断，失败原因见下方'
  return '任务已结束'
})
const analysisFootLabel = computed(() => {
  if (parseJobRunning.value) return '任务执行中'
  if (!flow.parseJobId) return '等待任务'
  if (parseJob.value?.status === 'succeeded') return '任务已完成'
  return '任务已结束'
})
/** 从 JSON 文本片段中按 key 统计数组元素数量 */
function countJsonArrayItems(prefix: string, text: string): number {
  const idx = text.indexOf(prefix)
  if (idx < 0) return 0
  const after = text.slice(idx + prefix.length)
  let depth = 0
  let started = false
  let count = 0
  for (let i = 0; i < after.length; i++) {
    const c = after[i]
    if (c === '{') { depth++; started = true }
    else if (c === '}') { depth--; if (depth <= 0 && started) break }
    else if (c === '[') { depth++; started = true }
    else if (c === ']') { depth--; if (depth <= 0 && started) break }
    // 统计顶层对象的出现次数（每个顶层对象以 "{ 开头）
    if (started && depth === 1 && c === '{' && (i === 0 || after[i-1] !== '\\')) {
      let j = i + 1
      while (j < after.length && after[j] !== '"' && after[j] !== '}') j++
      if (after[j] === '"') count++
    }
  }
  // 修正：用简单的正则匹配闭合对象计数更可靠
  const section = after.slice(0, 50000)
  // 统计独立顶层对象的名称（如 "name": "xxx" 模式）
  const nameMatches = section.match(/"name"\s*:\s*"/g)
  return nameMatches ? nameMatches.length : 0
}

/** 将 LLM 输出的 JSON 流转换为人性化进度描述 */
const readableStreamText = computed(() => {
  const raw = llmStreamText.value
  if (!raw) return ''
  // 检测是否像 JSON 输出（json_mode 下的 LLM 流）
  if (!raw.trimStart().startsWith('{') && !raw.includes('"characters"') && !raw.includes('"scenes"')) {
    return raw // 不是 JSON 流，直接展示原文
  }
  const lines: string[] = []
  // 提取 episode 信息
  const epMatch = raw.match(/"episode"\s*:\s*(\d+)/g)
  if (epMatch) {
    const nums = epMatch.map(m => m.match(/\d+/)?.[0] ?? '').filter(Boolean)
    if (nums.length) lines.push(`📺 正在分析第 ${nums.join('、')} 集`)
  }
  // 统计角色数量
  const charCount = countJsonArrayItems('"characters"', raw)
  if (charCount > 0) lines.push(`👤 已识别 ${charCount} 个角色`)
  // 统计场景数量
  const sceneCount = countJsonArrayItems('"scenes"', raw)
  if (sceneCount > 0) lines.push(`🎬 已识别 ${sceneCount} 个场景`)
  // 统计分镜数量
  const shotCount = countJsonArrayItems('"shots"', raw)
  if (shotCount > 0) lines.push(`🎞️ 已拆解 ${shotCount} 个分镜`)
  // 统计道具数量
  const propCount = countJsonArrayItems('"props"', raw)
  if (propCount > 0) lines.push(`🪑 已识别 ${propCount} 个道具`)
  // 评估相关
  if (raw.includes('"score"')) {
    const scoreMatch = raw.match(/"score"\s*:\s*([\d.]+)/)
    if (scoreMatch) lines.push(`📊 剧本评分 ${scoreMatch[1]}`)
  }
  // 剧集梗概
  const titleMatches = raw.match(/"title"\s*:\s*"([^"]+)"/g)
  if (titleMatches && titleMatches.length > 0) {
    const titles = titleMatches.map(m => m.match(/"([^"]+)"$/)?.[1] ?? '').filter(Boolean)
    if (titles.length) lines.push(`📖 剧集：${titles.join('、')}`)
  }
  return lines.length > 0 ? lines.join('\n') : 'AI 正在输出分析内容…'
})

/** 输出区内容：有真实流式文本就展示，否则给与状态相符的说明（不伪造进度） */
const analysisBodyText = computed(() => {
  if (llmStreamText.value) return readableStreamText.value
  if (parseJobRunning.value) return llmPlaceholder.value
  if (parseJob.value?.status === 'succeeded') return '分析已完成。下方可查看解析结果与集数状态。'
  if (parseJob.value?.status === 'failed') return '本次分析未产出结果。请查看失败原因后重试。'
  return '等待 Agent 开始输出…'
})
/** 失败原因按文案分组：同一错误只展示一次，避免逐集重复刷屏 */
const failedGroups = computed(() => {
  const map = new Map<string, { eps: number[]; attempts: number }>()
  for (const e of failedEpisodes.value) {
    const key = e.error_msg || '解析失败，未返回具体原因'
    const cur = map.get(key)
    if (cur) {
      cur.eps.push(e.episode_no)
      cur.attempts = Math.max(cur.attempts, e.attempt_count || 0)
    } else {
      map.set(key, { eps: [e.episode_no], attempts: e.attempt_count || 0 })
    }
  }
  return [...map.entries()].map(([msg, v]) => ({ msg, eps: v.eps.sort((a, b) => a - b), attempts: v.attempts }))
})
/** 解析原文弹窗：低频内容不再占用内容区，改从页头按钮打开 */
const showRawReport = ref(false)
const copiedRaw = ref(false)
/** 传统复制：不依赖安全上下文与用户激活，作为 clipboard API 的回退 */
function legacyCopy(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.setAttribute('readonly', '')
  ta.style.position = 'fixed'
  ta.style.top = '-1000px'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(ta)
  return ok
}
/**
 * 复制解析全文。
 * 先试 navigator.clipboard（需安全上下文 + 用户激活），失败再回退 execCommand；
 * 站点常以 http 对外暴露，只写 clipboard 会在部分环境点不动。
 */
async function copyRawReport() {
  const text = rawReportText.value
  if (!text) return
  let ok = false
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      ok = true
    } catch {
      ok = false
    }
  }
  if (!ok) ok = legacyCopy(text)
  if (!ok) {
    message.error('复制失败，请手动选中文本复制')
    return
  }
  copiedRaw.value = true
  message.success('已复制解析全文')
  window.setTimeout(() => {
    copiedRaw.value = false
  }, 2000)
}
</script>

<template>
  <section class="parse-page">
    <header class="parse-header">
      <div class="parse-header-left">
        <h1 class="visually-hidden">解析</h1>
        <FlowNav :project-id="projectId" />
      </div>
      <div class="parse-header-actions">
        <NButton v-if="rawReportText" quaternary @click="showRawReport = true">
          解析原文
        </NButton>
        <NButton quaternary @click="router.push(`/project/${projectId}/script`)">返回剧本</NButton>
        <NButton
          v-if="pendingEpisodes.length && !parseJobRunning"
          :loading="continuing"
          @click="doContinueAgent()"
        >
          继续分析（{{ pendingEpisodes.length }} 集未完成）
        </NButton>
        <NButton
          v-if="flow.selectedEpisodes.length"
          :type="statusTotalEpisodes && flow.selectedEpisodes.length && !flow.candidateVersionId && !failedEpisodes.length ? 'primary' : 'default'"
          :disabled="!flow.scriptVersionId || !statusTotalEpisodes || !flow.selectedEpisodes.length || !!flow.candidateVersionId || submitting"
          :loading="parsing"
          @click="handleParse()"
        >
          解析选中（{{ flow.selectedEpisodes.length }} 集）
        </NButton>
      </div>
    </header>
    <div class="parse-body">
      <div class="content-pane">
        <!-- 剧本智能分析：状态 + 实时输出 + 真实耗时，统一在一个窗口内 -->
        <section v-if="analysisVisible" class="analysis-card">
          <header class="ac-head">
            <span class="ac-title">
              <n-icon :component="SparklesOutline" :size="15" />
              AI Agent
            </span>
            <span class="ac-state" :class="`is-${analysisState.tone}`">
              <i v-if="parseJobRunning" class="spin-dot" />
              {{ analysisState.label }}
            </span>
          </header>

          <div class="ac-meta">
            <span class="ac-stage">{{ analysisStage }}</span>
            <span v-if="statusTotalEpisodes" class="ac-progress">
              {{ statusSummary.succeeded }}/{{ statusTotalEpisodes }} 集
            </span>
            <span class="ac-elapsed" :title="`任务开始时间：${parseJob?.started_at || parseJob?.created_at || '—'}`">
              已耗时 {{ elapsedText }}
            </span>
          </div>

          <div class="ac-divider" />

          <div class="ac-body-label">Agent 实时输出</div>
          <div ref="llmStreamBoxRef" class="ac-body">{{ analysisBodyText }}</div>

          <div class="ac-divider" />

          <footer class="ac-foot">
            <span>{{ analysisFootLabel }}</span>
            <span v-if="parseJobRunning" class="ac-live">实时更新</span>
          </footer>

          <!-- 分支动作：仅在各自真实业务状态下出现，保持原入口能力 -->
          <div v-if="candidateReady && agentHasMore" class="ac-actions">
            <span class="ac-action-hint">
              已解析 {{ statusSummary.succeeded }}/{{ statusTotalEpisodes }} 集，还有 {{ pendingEpisodes.length }} 集待继续
            </span>
            <NButton size="small" type="primary" :loading="continuing" @click="doContinueAgent()">继续分析</NButton>
          </div>
          <div v-else-if="parseJobLost" class="ac-actions">
            <span class="ac-action-hint">任务状态已失效，需重新发起分析</span>
            <NButton size="small" @click="router.push(`/project/${projectId}/script`)">返回剧本</NButton>
          </div>
          <!-- 失败原因不在此重复：集级原因与重试入口统一由下方失败面板承担 -->
        </section>

        <!-- 解析原文：低频内容，改由页头「解析原文」按钮打开弹窗查看，
             不再在内容区常驻一条折叠条（曾占 47px 高 + 20px 间距） -->

        <!-- 集数解析状态：分析进行中不展示，分析完成后才显示 -->
        <div v-if="statusTotalEpisodes && !parseJobRunning" class="parse-status-panel panel-card">
          <div class="episode-toolbar">
            <span class="pane-label">分集解析结果</span>
            <span class="episode-count-tag">{{ statusSummary.succeeded }}/{{ statusTotalEpisodes || 0 }} 集完成</span>
            <span v-if="flow.selectedEpisodes.length" class="episode-count-tag is-selected">选中 {{ flow.selectedEpisodes.length }} 集</span>
            <span class="toolbar-spacer" />
            <button
              v-if="failedEpisodes.length"
              class="quick-chip"
              :class="{ active: showFailedOnly }"
              @click="showFailedOnly = !showFailedOnly"
            >
              只看失败（{{ statusSummary.failed }}）
            </button>
            <NButton
              v-if="failedEpisodes.length"
              size="small"
              type="primary"
              :loading="retrying"
              @click="doRetryParse()"
            >
              重试失败集（{{ failedEpisodes.length }}）
            </NButton>
          </div>
          <!-- 失败原因：与分集结果同处一块，不再单独成卡；相同错误按文案合并一次 -->
          <ul v-if="failedGroups.length" class="failure-list">
            <li v-for="g in failedGroups" :key="g.msg">
              <span class="failure-ep">第 {{ g.eps.join('、') }} 集</span>
              <span class="failure-msg">{{ g.msg }}</span>
              <span v-if="g.attempts" class="failure-attempt">已尝试 {{ g.attempts }} 次</span>
            </li>
          </ul>
          <!-- 集格与状态图例同排：集数少时图例靠右补位，避免右侧整片留白 -->
          <div class="episode-row">
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
              </div>
            </div>
            <div class="episode-legend">
              <span class="legend-item" :class="{ 'is-zero': !statusSummary.pending }"><i class="dot is-pending" />待解析 {{ statusSummary.pending }}</span>
              <span class="legend-item" :class="{ 'is-zero': !statusSummary.running }"><i class="dot is-running" />进行中 {{ statusSummary.running }}</span>
              <span class="legend-item" :class="{ 'is-zero': !statusSummary.succeeded }"><i class="dot is-succeeded" />已完成 {{ statusSummary.succeeded }}</span>
              <span class="legend-item" :class="{ 'is-zero': !statusSummary.failed }"><i class="dot is-failed" />失败 {{ statusSummary.failed }}</span>
            </div>
          </div>
        </div>

        <!-- 解析结果：状态 + 紧凑概览 + 分组内容，统计只在这里出现一次 -->
        <section v-if="hasResults || parseAllSucceeded" class="result-panel">
          <header class="result-head">
            <h3 class="result-title">解析结果</h3>
            <span class="result-state" :class="`is-${resultState.tone}`">{{ resultState.label }}</span>
            <span class="result-spacer" />
            <NButton size="tiny" quaternary @click="router.push(`/project/${projectId}/storyboard-images`)">
              去分镜图 →
            </NButton>
            <NButton
              v-if="parseAllSucceeded"
              size="small"
              type="primary"
              @click="router.push(`/project/${projectId}/assets`)"
            >
              去资产库 →
            </NButton>
          </header>

          <div class="overview-stats">
            <component
              :is="s.group ? 'button' : 'div'"
              v-for="s in overviewStats"
              :key="s.key"
              class="ov-item"
              :class="{ 'is-clickable': !!s.group, 'is-current': !!s.group && activeGroup === s.group }"
              :type="s.group ? 'button' : undefined"
              @click="s.group ? (activeGroup = s.group) : undefined"
            >
              <span class="ov-value">{{ s.value }}</span>
              <span class="ov-label">{{ s.label }}</span>
            </component>
            <div v-if="pendingIssueCount" class="ov-item is-issue">
              <span class="ov-value">{{ pendingIssueCount }}</span>
              <span class="ov-label">待处理</span>
            </div>
          </div>

          <div class="result-body">
            <nav class="group-nav" aria-label="解析结果分组">
              <button
                v-for="g in resultGroups"
                :key="g.key"
                type="button"
                class="group-item"
                :class="{ 'is-active': activeGroup === g.key }"
                :aria-current="activeGroup === g.key ? 'true' : undefined"
                @click="activeGroup = g.key"
              >
                <span class="group-label">{{ g.label }}</span>
                <span class="group-count" :class="{ 'is-text': g.count === null }">{{ g.count === null ? '未生成' : g.count }}</span>
              </button>
            </nav>

            <div class="group-pane" role="region" :aria-label="activeGroupLabel">
              <template v-if="activeGroup === 'scene'">
                <ol v-if="scenes.length" class="result-scenes is-tall">
                  <li v-for="sc in scenes" :key="sc.id">
                    <span class="rs-seq">#{{ sc.seq }}</span>
                    <span class="rs-body">
                      <span class="rs-title" :title="sc.title">{{ sc.title }}</span>
                      <span v-if="sc.summary" class="rs-desc" :title="sc.summary">{{ sc.summary }}</span>
                    </span>
                    <span v-if="sc.episodes?.length" class="rs-ep">第 {{ sc.episodes.join('、') }} 集</span>
                    <span class="rs-count">{{ sc.shot_count ?? 0 }} 镜</span>
                  </li>
                </ol>
                <p v-else class="pane-empty">未解析出场景</p>
              </template>

              <template v-else-if="activeGroup === 'character'">
                <ul v-if="characters.length" class="result-people is-tall">
                  <li v-for="c in characters" :key="c.id">
                    <span class="rp-name">{{ c.name }}</span>
                    <span v-if="c.description" class="rp-desc" :title="c.description">{{ c.description }}</span>
                  </li>
                </ul>
                <p v-else class="pane-empty">未解析出角色</p>
              </template>

              <template v-else-if="activeGroup === 'prop'">
                <ul v-if="props.length" class="result-people is-tall">
                  <li v-for="pr in props" :key="pr.id">
                    <span class="rp-name">{{ pr.name }}</span>
                    <span v-if="pr.description" class="rp-desc" :title="pr.description">{{ pr.description }}</span>
                  </li>
                </ul>
                <p v-else class="pane-empty">未解析出道具</p>
              </template>

              <template v-else-if="activeGroup === 'shot'">
                <div v-if="parseShots.length" class="pane-table">
                  <div class="pt-row pt-head" aria-hidden="true">
                    <span class="pt-no">场-镜</span>
                    <span class="pt-scene">场景</span>
                    <span class="pt-type">景别</span>
                    <span class="pt-dur">时长</span>
                    <span class="pt-desc">画面</span>
                  </div>
                  <div
                    v-for="s in parseShots"
                    :key="`${s.ep}-${s.sceneSeq}-${s.shotNo}`"
                    class="pt-row"
                  >
                    <span class="pt-no">{{ s.sceneSeq }}-{{ s.shotNo }}</span>
                    <span class="pt-scene" :title="s.sceneTitle">第 {{ s.ep }} 集 · {{ s.sceneTitle }}</span>
                    <span class="pt-type">{{ s.shotType || '—' }}</span>
                    <span class="pt-dur">{{ s.duration == null ? '—' : `${s.duration}s` }}</span>
                    <span class="pt-desc" :title="s.desc">
                      {{ s.desc }}<em v-if="s.dialogue.length" class="pt-flag">台词 {{ s.dialogue.length }}</em>
                    </span>
                  </div>
                </div>
                <p v-else class="pane-empty">
                  {{
                    shotsWithoutDetail
                      ? `解析结果明细已不可用；库里当前有 ${totalShots} 个分镜，请到分镜图页查看`
                      : '未解析出分镜'
                  }}
                </p>
                <div class="pane-foot">
                  <NButton size="tiny" quaternary @click="router.push(`/project/${projectId}/storyboard-images`)">
                    去分镜图查看全部 →
                  </NButton>
                </div>
              </template>

              <template v-else-if="activeGroup === 'dialogue'">
                <ul v-if="dialogueRows.length" class="dialog-list is-tall">
                  <li v-for="(d, i) in dialogueRows" :key="`${d.ep}-${d.shotNo}-${i}`">
                    <span class="dl-meta">第 {{ d.ep }} 集 · #{{ d.shotNo }}</span>
                    <span class="dl-speaker" :class="{ 'is-narration': d.narration }">
                      {{ d.speaker || (d.narration ? '旁白' : '未标注说话人') }}
                    </span>
                    <span class="dl-text">{{ d.text }}</span>
                    <span v-if="d.emotion" class="dl-emotion">{{ emotionLabel(d.emotion) }}</span>
                  </li>
                </ul>
                <p v-else class="pane-empty">
                  {{ shotsWithoutDetail ? '解析结果明细已不可用，台词明细请到分镜看板查看' : '未解析出台词' }}
                </p>
              </template>

              <template v-else-if="activeGroup === 'audio'">
                <div class="pane-note">
                  <p>
                    解析结果不含音频规划。配音、BGM、音效在资产与分镜确认后按台词生成；TTS 音频与 H3 视频参考槽位相互独立。
                  </p>
                  <NButton size="tiny" quaternary @click="router.push(`/project/${projectId}/assets`)">
                    去资产库 →
                  </NButton>
                </div>
              </template>
            </div>
          </div>
        </section>

        <NEmpty
          v-else-if="statusTotalEpisodes > 0 && statusSummary.pending === statusTotalEpisodes"
          description="尚未解析，点击右上角『解析选中』提交任务"
        />
        <NEmpty v-else-if="statusTotalEpisodes === 0" description="尚未确定集数，请返回剧本页完成分析" />
      </div>
    </div>

    <!-- 解析原文弹窗：低频核对内容，按需打开，不再常驻占用内容区 -->
    <NModal
      v-model:show="showRawReport"
      preset="card"
      title="解析原文（评估与拆解全文）"
      class="raw-report-modal"
      :bordered="false"
      :style="{ width: 'min(920px, 92vw)' }"
    >
      <template #header-extra>
        <NButton size="tiny" quaternary @click="copyRawReport">
          {{ copiedRaw ? '已复制' : '复制全文' }}
        </NButton>
      </template>
      <pre class="raw-report-modal-body">{{ rawReportText }}</pre>
    </NModal>
  </section>
</template>

<style scoped>
/* ===== 失败原因：并入分集结果面板，用左侧色条标记而非整块红底 ===== */
.toolbar-spacer { flex: 1; }
.failure-list {
  list-style: none;
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-left: 2px solid var(--danger-line);
  background: var(--danger-soft);
  border-radius: var(--radius-sm);
}
.failure-list li { display: flex; align-items: baseline; gap: var(--sp-2); font-size: 12.5px; line-height: 1.6; }
.failure-ep { flex: none; font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.failure-msg { flex: 1; min-width: 0; color: var(--text-secondary); word-break: break-word; }
.failure-attempt { flex: none; font-size: 11.5px; color: var(--text-tertiary); }

/* ===== 解析原文弹窗正文 ===== */
.raw-report-modal-body {
  margin: 0;
  padding: var(--sp-3) var(--sp-4);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: min(62vh, 560px);
  overflow-y: auto;
  font-family: var(--font-mono);
}
.parse-page {
  --parse-col-max: 960px;
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}
/* 页头 / 页身：使用页面私有类名，避免命中 global.css 的 .page-header / .page-body
   （全局 .page-body 带 max-width + margin:0 auto，在 flex 容器里会把内容压成窄柱） */
.parse-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  /* 左右内边距跟随内容列限宽，让页头两端与卡片边缘对齐，避免页头飘在更外侧 */
  padding: 10px max(20px, calc((100% - var(--parse-col-max)) / 2));
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.parse-header-left { display: flex; align-items: center; gap: 10px; min-width: 0; }

.parse-header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.parse-body { display: flex; flex: 1; min-height: 0; min-width: 0; }
.content-pane { flex: 1; min-width: 0; padding: 24px 20px 40px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; }
/* 内容列限宽居中：避免超宽屏下卡片内部左右两端（标题 / 操作按钮）被拉开过远。
   限宽加在卡片自身而非 .content-pane 上，防止 flex:1 + margin:auto 把内容压成窄柱。 */
.content-pane > * {
  width: 100%;
  max-width: var(--parse-col-max);
  margin-inline: auto;
}
/* ===== 剧本智能分析：单一窗口承载状态 / 输出 / 耗时 ===== */
.analysis-card {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  overflow: hidden;
}
.ac-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  background: var(--bg-sunken);
  border-bottom: 1px solid var(--border-subtle);
}
.ac-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.ac-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  padding: 1px 8px;
  font-size: 12px;
  line-height: 19px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--neutral-line);
  background: var(--neutral-soft);
  color: var(--text-secondary);
  white-space: nowrap;
}
.ac-state.is-running { color: var(--brand-hover); background: var(--brand-soft); border-color: var(--brand-line); }
.ac-state.is-succeeded { color: var(--state-success); background: var(--success-soft); border-color: var(--success-line); }
.ac-state.is-failed { color: var(--danger-text); background: var(--danger-soft); border-color: var(--danger-line); }
.ac-meta {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4) 0;
  font-size: 12.5px;
}
.ac-stage { flex: 1; min-width: 0; color: var(--text-secondary); }
.ac-progress { flex: none; font-variant-numeric: tabular-nums; color: var(--text-tertiary); }
.ac-elapsed {
  flex: none;
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}
.ac-divider { height: 1px; margin: var(--sp-3) var(--sp-4) 0; background: var(--border-subtle); }
.ac-body-label {
  padding: var(--sp-3) var(--sp-4) 0;
  font-size: 11.5px;
  letter-spacing: 0.03em;
  color: var(--text-tertiary);
}
.ac-body {
  max-height: 320px;
  overflow-y: auto;
  padding: var(--sp-2) var(--sp-4) var(--sp-3);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}
.ac-foot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4);
  font-size: 12px;
  color: var(--text-tertiary);
  background: var(--bg-sunken);
  border-top: 1px solid var(--border-subtle);
}
.ac-live { margin-left: auto; color: var(--brand-hover); }
.ac-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border-subtle);
}
.ac-action-hint { flex: 1; min-width: 0; font-size: 12.5px; color: var(--text-secondary); word-break: break-word; }
.spin-dot { width: 10px; height: 10px; border: 2px solid currentColor; border-top-color: transparent; border-radius: 50%; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.parse-status-panel { border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: 18px; display: flex; flex-direction: column; gap: 14px; }
.episode-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pane-label { font-size: 16px; font-weight: 600; color: var(--text-primary); }
.episode-count-tag { font-size: 14px; font-weight: 600; color: var(--brand-primary); font-variant-numeric: tabular-nums; }
.episode-count-tag.is-selected { background: var(--brand-soft); border-radius: var(--radius-sm); padding: 0 6px; }
.quick-chip { border: 1px solid var(--border-subtle); background: none; border-radius: var(--radius-pill); padding: 3px 12px; font-size: 12px; color: var(--text-secondary); cursor: pointer; transition: all 0.15s ease; }
.quick-chip:hover:not(:disabled) { border-color: var(--brand-primary); color: var(--brand-primary); }
.quick-chip.active { background: var(--brand-soft); border-color: var(--brand-primary); color: var(--brand-primary); }
.quick-chip:disabled { opacity: 0.4; cursor: not-allowed; }
/* 集格固定宽度流式换行，图例紧跟其后；不用 1fr 拉伸，避免单集时格子被拉宽、图例被推到最右 */
.episode-row { display: flex; align-items: center; gap: var(--sp-3) var(--sp-6); flex-wrap: wrap; }
.episode-grid { display: flex; flex-wrap: wrap; gap: 12px; min-width: 0; }
.episode-cell { flex: 0 0 140px; width: 140px; position: relative; min-height: 84px; padding: 14px 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; border-radius: var(--radius-lg); border: 1px solid transparent; transition: transform 0.15s ease; }
.episode-cell:hover { transform: translateY(-1px); }
.episode-cell-no { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.2; white-space: nowrap; }
.episode-cell-meta { font-size: 14px; line-height: 1.3; opacity: 0.95; white-space: nowrap; }
@keyframes cell-pulse { 0%, 100% { box-shadow: 0 0 0 0 var(--brand-line); } 50% { box-shadow: 0 0 0 4px var(--brand-soft); } }
.episode-cell.is-running { animation: cell-pulse 1.6s ease-in-out infinite; }
.episode-cell.is-pending { background: var(--neutral-soft); color: var(--text-secondary); }
.episode-cell.is-running { background: var(--brand-soft); color: var(--brand-primary); border-color: var(--brand-line); }
.episode-cell.is-succeeded { background: var(--success-soft); color: var(--state-success); border-color: var(--success-line); }
.episode-cell.is-failed { background: var(--danger-soft); color: var(--brand-error); border-color: var(--danger-line); }
/* 失败原因统一在顶部失败面板展示，集格内不再重复重试入口 */
.episode-legend { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; font-size: 14px; color: var(--text-secondary); flex: none; }
.legend-item { display: inline-flex; align-items: center; gap: 4px; }
/* 计数为 0 的状态降权：色点去饱和、文字转弱，避免「失败 0」的红点抢视觉焦点 */
.legend-item.is-zero { opacity: 0.5; }
.legend-item.is-zero .dot { background: var(--neutral-line) !important; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.is-pending { background: var(--neutral-line); }
.dot.is-running { background: var(--brand-primary); }
.dot.is-succeeded { background: var(--success-line); }
.dot.is-failed { background: var(--brand-error); }
@media (max-width: 768px) {
  .episode-cell { flex: 0 0 88px; width: 88px; min-height: 68px; padding: 10px 6px; }
  .episode-cell-no { font-size: 14px; }
  .episode-cell-meta { font-size: 12px; }
  .parse-header { flex-wrap: wrap; gap: var(--sp-2); padding: 10px 12px; }
  .parse-header-left { width: 100%; }
  /* 移动端操作区独占一行，按钮等分宽度，避免第三个按钮被裁切 */
  .parse-header-actions { width: 100%; flex-wrap: nowrap; gap: var(--sp-2); }
  .parse-header-actions > * { flex: 1 1 0; min-width: 0; }
  .ac-head { flex-wrap: wrap; }
  .ac-meta { flex-wrap: wrap; row-gap: 2px; }
  .ac-actions { flex-wrap: wrap; }
  .ac-body { max-height: 240px; }
  .content-pane { padding: var(--sp-4) var(--sp-3); gap: var(--sp-4); }
}

/* 解析结果总览：场景清单 + 角色 / 道具 chips
   卡片间距统一由 .content-pane 的 gap 控制，这里不再叠加 margin-top */
.result-panel {
  padding: var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.result-head {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  row-gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-3);
}
.result-title {
  margin: 0;
  flex: none;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.result-state {
  padding: 1px 7px;
  font-size: 11.5px;
  font-weight: 500;
  line-height: 18px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--neutral-line);
  color: var(--text-secondary);
  background: var(--neutral-soft);
  white-space: nowrap;
}
.result-state.is-succeeded { color: var(--state-success); background: var(--success-soft); border-color: var(--success-line); }
.result-state.is-running { color: var(--brand-hover); background: var(--brand-soft); border-color: var(--brand-line); }
.result-state.is-warning { color: var(--state-warning); background: var(--warning-soft); border-color: var(--warning-line); }
.result-state.is-failed { color: var(--danger-text); background: var(--danger-soft); border-color: var(--danger-line); }
.result-spacer { flex: 1; }

/* 紧凑统计条：统计只出现一次，不再是六张大卡片 */
.overview-stats {
  display: flex;
  align-items: stretch;
  gap: 0;
  margin-bottom: var(--sp-4);
  padding: var(--sp-3) 0;
  border-top: 1px solid var(--border-subtle);
  border-bottom: 1px solid var(--border-subtle);
  flex-wrap: wrap;
}
.ov-item {
  display: flex;
  align-items: baseline;
  gap: 5px;
  padding: 0 var(--sp-4);
  /* 统计项可能是 <button>（可点击切分组）：清掉 UA 默认外观，保证与 <div> 视觉一致 */
  border: 0;
  border-right: 1px solid var(--border-subtle);
  background: transparent;
  font-family: inherit;
  text-align: left;
  color: inherit;
  cursor: default;
}
.ov-item.is-clickable { cursor: pointer; }
.ov-item.is-clickable:hover .ov-value { color: var(--brand-hover); }
.ov-item.is-current .ov-value { color: var(--brand-hover); }
.ov-item:focus-visible { outline: 2px solid var(--brand-hover); outline-offset: 2px; }
.ov-item:first-child { padding-left: 0; }
.ov-item:last-child, .ov-item.is-issue { border-right: none; }
.ov-value {
  font-size: 19px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.ov-label { font-size: 12px; color: var(--text-tertiary); }
.ov-item.is-issue .ov-value { color: var(--danger-text); }
.ov-item.is-issue .ov-label { color: var(--danger-text); }

/* ===== 分组目录 + 内容面板（替代原三栏平铺） ===== */
.result-body {
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  gap: var(--sp-5);
  align-items: start;
}
.group-nav {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding-right: var(--sp-3);
  border-right: 1px solid var(--border-subtle);
}
.group-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: 6px var(--sp-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-secondary);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.group-item:hover { background: var(--overlay-hover); color: var(--text-primary); }
.group-item.is-active { background: var(--brand-soft); color: var(--brand-hover); font-weight: 600; }
.group-item:focus-visible { outline: 2px solid var(--brand-hover); outline-offset: 1px; }
.group-count {
  flex: none;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
/* 无数字的占位（如「音频规划 未生成」）用更小的字号，避免和真实计数一样抢眼 */
.group-count.is-text { font-size: 10.5px; letter-spacing: 0.02em; }
.group-item.is-active .group-count { color: var(--brand-hover); }
.group-pane { min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.pane-empty { margin: 0; padding: var(--sp-4) 0; font-size: 12.5px; color: var(--text-tertiary); }
.pane-foot { display: flex; justify-content: flex-end; }
.pane-note { display: flex; flex-direction: column; align-items: flex-start; gap: var(--sp-2); padding: var(--sp-3) 0; }
.pane-note p { margin: 0; max-width: 62ch; font-size: 12.5px; line-height: 1.7; color: var(--text-secondary); }
.is-tall { max-height: 460px; }

/* 分镜表：列由内容决定，镜号/景别/时长定宽，画面自适应 */
.pane-table { display: flex; flex-direction: column; max-height: 460px; overflow-y: auto; }
.pt-row {
  display: grid;
  grid-template-columns: 56px minmax(120px, 1.1fr) 64px 56px minmax(0, 2.6fr);
  gap: var(--sp-3);
  align-items: start;
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  line-height: 1.55;
}
.pt-row:not(.pt-head):hover { background: var(--overlay-hover); }
.pt-head {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
}
.pt-no { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.pt-scene { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); }
.pt-type, .pt-dur { font-size: 11.5px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.pt-desc { min-width: 0; color: var(--text-primary); word-break: break-word; }
.pt-flag { margin-left: 6px; font-style: normal; font-size: 11px; color: var(--brand-hover); }

/* 对白：说话人与台词分列，情绪单独一列弱化 */
.dialog-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; overflow-y: auto; }
.dialog-list li {
  display: grid;
  grid-template-columns: 96px 96px minmax(0, 1fr) auto;
  gap: var(--sp-3);
  align-items: baseline;
  padding: 6px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  line-height: 1.6;
}
.dialog-list li:hover { background: var(--overlay-hover); }
.dl-meta { font-family: var(--font-mono); font-size: 11px; color: var(--text-tertiary); }
.dl-speaker { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; color: var(--text-primary); }
/* 旁白不是角色，弱化并与角色台词区分（不只靠颜色，标签文字本身就是「旁白」） */
.dl-speaker.is-narration { font-weight: 400; color: var(--text-tertiary); }
.dl-text { min-width: 0; color: var(--text-primary); word-break: break-word; }
.dl-emotion { flex: none; font-size: 11px; color: var(--text-tertiary); }

.result-scenes, .result-people {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
  overflow-y: auto;
}
.result-scenes li, .result-people li {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  line-height: 1.55;
}
.result-scenes li:hover, .result-people li:hover { background: var(--overlay-hover); }
.rs-seq {
  flex: none;
  padding-top: 1px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
}
.rs-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.rs-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}
.rs-desc, .rp-desc {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11.5px;
  color: var(--text-tertiary);
}
.rs-ep {
  flex: none;
  padding-top: 1px;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.rs-count {
  flex: none;
  padding-top: 1px;
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.rp-name { flex: none; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
@media (max-width: 1100px) {
  .result-body { grid-template-columns: 168px minmax(0, 1fr); }
  .pt-row { grid-template-columns: 52px minmax(104px, 1fr) 56px 52px minmax(0, 2.4fr); gap: var(--sp-2); }
}
@media (max-width: 768px) {
  /* 目录转横向 Tab；分镜/对白收成两行一条，避免小屏挤压 */
  .result-body { grid-template-columns: 1fr; gap: var(--sp-3); }
  .group-nav {
    flex-direction: row;
    overflow-x: auto;
    padding: 0 0 var(--sp-2);
    border-right: 0;
    border-bottom: 1px solid var(--border-subtle);
  }
  .group-item { flex: none; padding: 6px var(--sp-3); }
  .is-tall, .pane-table, .dialog-list { max-height: none; }
  .pt-head { display: none; }
  .pt-row { grid-template-columns: 40px minmax(0, 1fr); gap: 0 var(--sp-2); }
  .pt-no { grid-row: 1 / span 2; }
  .pt-scene, .pt-desc { grid-column: 2; }
  .pt-type, .pt-dur { display: none; }
  .dialog-list li { grid-template-columns: 1fr; gap: 1px; }
  .dl-speaker { font-weight: 600; }
  /* 统计条按 3 列均分，避免单个统计项被挤到第二行孤零零 */
  .overview-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--sp-3) 0; }
  .ov-item { padding: 0 var(--sp-3); }
  .ov-item:nth-child(3n) { border-right: none; }
  .result-head { align-items: flex-start; }
  .result-spacer { display: none; }
  .result-head > button { flex: none; }
}
@media (max-width: 480px) {
  /* 手机上 6 个分组挤不进一行：允许换行，避免后两个分组被藏在横向滚动里 */
  .group-nav { flex-wrap: wrap; overflow-x: visible; }
}
</style>
