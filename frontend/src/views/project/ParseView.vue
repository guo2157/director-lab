<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, NIcon, NModal, useMessage } from 'naive-ui'
import { SparklesOutline } from '@vicons/ionicons5'
import {
  confirmParseVersion,
  getCurrentScript,
  getParseStatus,
  getProject,
  getScriptAgentStatus,
  getScriptReports,
  retryParse,
  runScriptAgent,
} from '@/api/projects'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { cancelJob, getJob, listJobs } from '@/api/jobs'
import type { Job } from '@/types/job'
import type { EpisodeParseStatusItem, ParseGateResult, Scene, ScriptReports, Shot } from '@/types/script'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => String(route.params.id ?? ''))

/**
 * 解析页状态全部由「后端事实 + 本页局部状态」组成，不再依赖 scriptFlow store：
 * 该 store 未持久化，刷新即丢，而解析页必须能直接访问 /project/:id/parse 得到
 * 正确状态（解析已是独立制作节点）。运行中的任务通过 getScriptAgentStatus 恢复。
 */
const parseJobId = ref<string | null>(null)
const candidateVersionId = ref<string | null>(null)
/** 确认后返回的生产准入结果，null=无报告/READY */
const gateResult = ref<ParseGateResult | null>(null)

/* -- P1-9: three independent reports -- */
const scriptReports = ref<ScriptReports | null>(null)
const reportsLoading = ref(false)
async function fetchReports() {
  if (!projectId.value) return
  reportsLoading.value = true
  try {
    const res = await getScriptReports(projectId.value)
    scriptReports.value = res.reports
  } catch {
    scriptReports.value = null
  } finally {
    reportsLoading.value = false
  }
}
function reportStatusLabel(s: string): string {
  const m: Record<string, string> = { passed: '通过', partial: '部分通过', failed: '未通过', skipped: '未评估' }
  return m[s] ?? s
}
function reportStatusClass(s: string): string {
  if (s === 'passed' || s === 'skipped') return 'status-pass'
  if (s === 'partial') return 'status-warn'
  return 'status-fail'
}

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

/* ---------- 剧本版本（解析一律以已保存版本为准） ---------- */
const { data: scriptData } = useQuery({
  queryKey: computed(() => ['currentScript', projectId.value]),
  queryFn: () => getCurrentScript(projectId.value),
  enabled: computed(() => !!projectId.value),
})
const scriptVersionId = computed(() => scriptData.value?.script_version_id ?? null)
const scriptText = computed(() => scriptData.value?.text ?? '')
const hasScript = computed(() => Boolean(scriptData.value?.exists && scriptVersionId.value))


/* ---------- 发起解析（本页是唯一入口，不再依赖剧本页跳转） ---------- */
const submittedEpsLabel = ref('')
/**
 * 开始解析：把已保存版本交给 script-agent（评估 + 解析一体化，集数由 Agent 决定）。
 * 为什么用 script_agent 而不是 /parse：后者要求先有集数评估结果，而评估入口已下线；
 * script-agent 允许 episode_start 由前端指定，天然支持「首批 + 继续」两段式。
 */
const { mutate: startParse, isPending: starting } = useMutation({
  mutationFn: (episodeStart?: number) => {
    const vid = scriptVersionId.value
    if (!vid) throw new Error('尚未保存剧本版本')
    return runScriptAgent(projectId.value, vid, scriptText.value, episodeStart)
  },
  onSuccess: (res) => {
    parseJobId.value = res.job_id
    void queryClient.invalidateQueries({ queryKey: ['parse-status', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
    message.success('已开始解析，Agent 将自动完成评估与拆解')
  },
  onError: (e) => message.error(errMsg(e)),
})

/** 首次解析：无任何集级记录时从第 1 集开始 */
function handleStartParse() {
  if (!hasScript.value || starting.value) return
  submittedEpsLabel.value = '全部集'
  startParse(undefined)
}

/* ---------- 解析任务轮询 ---------- */
const { data: parseJobData, isError: parseJobError } = useQuery({
  queryKey: computed(() => ['job', parseJobId.value]),
  queryFn: () => getJob(parseJobId.value!),
  enabled: computed(() => !!parseJobId.value),
  refetchInterval: (query) => {
    const data = query.state.data
    return data && (data.status === 'queued' || data.status === 'running') ? 2000 : false
  },
})
const parseJob = computed(() => parseJobData.value ?? null)
const parseJobRunning = computed(
  () =>
    !!parseJobId.value &&
    !parseJobError.value &&
    (!parseJob.value || parseJob.value.status === 'queued' || parseJob.value.status === 'running'),
)
/**
 * 耗时以任务真实时间戳为准，不再本地自增：
 * - 起点 priority：started_at（任务真正开始执行）> created_at（排队也计入，但至少不会归零）
 * - 终点：finished_at 存在即冻结，否则取当前时间
 * 这样刷新页面、重新渲染都不会重置计时，任务结束后也不会继续跳。
 *
 * 时区：后端历史数据里有不带时区后缀的 naive UTC 串（如 2026-11-20T13:16:34）。
 * `new Date()` 会把它当**本地时间**解释，在 CST 下凭空多出 8 小时
 * （实测「刚点开始解析就显示 08:00:26」）。这里对无时区串显式按 UTC 解析，
 * 与后端新增的 +00:00 序列化双保险。
 */
function parseServerTime(raw: string | null | undefined): number {
  if (!raw) return 0
  // 无时区标记（无 Z、无 ±hh:mm）→ 视为 UTC，避免被当成本地时间
  const hasTz = /(?:Z|[+-]\d{2}:?\d{2})$/.test(raw)
  const ms = new Date(hasTz ? raw : raw + 'Z').getTime()
  return Number.isNaN(ms) ? 0 : ms
}
const nowTick = ref(Date.now())
const analysisStartedMs = computed(() => {
  const j = parseJob.value
  const ms = parseServerTime(j?.started_at || j?.created_at)
  return ms
})
const parseElapsed = computed(() => {
  const start = analysisStartedMs.value
  if (!start) return 0
  const fin = parseJob.value?.finished_at
  const end = fin ? parseServerTime(fin) : nowTick.value
  if (!end) return 0
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
const parseJobLost = computed(() => !!parseJobId.value && parseJobError.value)
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
/** 后端上报的真实阶段（agent.stage 事件）。前端不再按 JSON 猜阶段。 */
const agentStage = ref<{ stage: string; label: string } | null>(null)
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
  // 1. 先回放历史：llm-stream 接口返回已累积文本、最新 seq 与最近一次阶段
  try {
    const res = await fetch(`/api/jobs/${jobId}/llm-stream`)
    if (res.ok) {
      const data = await res.json()
      llmStreamText.value = data.text || ''
      llmLastSeq = data.last_seq || 0
      if (data.stage?.label) {
        agentStage.value = { stage: String(data.stage.stage || ''), label: String(data.stage.label) }
      }
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
        } else if (data.event_type === 'agent.stage' && data.id === jobId && data.label) {
          // 后端真实阶段：解析开始后前几十秒没有 token，靠它告诉用户"在做什么"
          agentStage.value = { stage: String(data.stage || ''), label: String(data.label) }
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
  [() => parseJobId.value, parseJobRunning],
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
/**
 * Agent 任务状态：必须声明在 `parsePersisted` / `needsPersistPoll` 之前。
 *
 * vue-query 会在 setup 阶段同步求值 `refetchInterval`（函数形式），而
 * `characters` / `props` 两个查询的 refetchInterval 会调用 needsPersistPoll →
 * parsePersisted → agentStatusData。若本声明留在文件末尾，客户端路由进入时
 * query 缓存已有 parse-status，`statusTotalEpisodes === 0` 的短路不再生效，
 * 于是访问到尚未初始化的 `agentStatusData`，抛
 * `ReferenceError: Cannot access 'fe' before initialization`（TDZ），
 * 整页渲染中断 —— 表现就是「从剧本页点去解析，解析页空白，刷新才正常」。
 * 全量刷新时缓存为空、短路生效，所以旧代码没暴露这个问题。
 */
const { data: agentStatusData } = useQuery({
  queryKey: computed(() => ['script-agent-status', projectId.value]),
  queryFn: () => getScriptAgentStatus(projectId.value),
  enabled: computed(() => !!projectId.value),
  // 始终轮询：后端可能还有另一个 running 任务（连点两次会留下 running + queued）。
  // 不能因为「已持有 jobId」就停掉轮询，否则永远不会发现真正的 running 任务。
  refetchInterval: 4000,
})
/**
 * 集号列表：唯一来源是后端锁定的总集数 + 已有解析记录的最大集号。
 * 评估页下线后前端不再持有「用户选中集」（scriptFlow.selectedEpisodes 已移除），
 * 解析范围由 Agent 自己决定，页面只负责把结果按集展示。
 */
const episodeNumbers = computed<number[]>(() => {
  const records = parseStatus.value?.records ?? []
  const maxRecorded = records.reduce((m, r) => Math.max(m, r.episode_no), 0)
  // 历史数据的 version.total_episodes 可能为空，此时用记录里的最大集号反推，
  // 否则集数会被算成 0，集数方格区与失败/重试入口整体消失。
  const total = parseStatus.value?.total_episodes ?? maxRecorded
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
/**
 * 集级状态是否「混杂」：只有混杂时状态图例才提供新信息。
 * 全部成功 → 集格统一绿底 + 标题旁「1/1 集完成」已经说清，图例是第三次复述；
 * 全部待解析 → 集格统一灰底，同理。
 */
const hasMixedEpisodeStatus = computed(() => {
  const s = statusSummary.value
  const nonZero = [s.pending, s.running, s.succeeded, s.failed].filter((n) => n > 0)
  return nonZero.length > 1
})
const { mutate: doRetryParse, isPending: retrying } = useMutation({
  mutationFn: () => retryParse(projectId.value),
  onSuccess: (res) => {
    parseJobId.value = res.job_id
    void queryClient.invalidateQueries({ queryKey: ['parse-status', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
    message.success(`已重新入队 ${res.retried_episodes.length} 个失败集`)
  },
  onError: (e) => message.error(errMsg(e)),
})
/* ---------- Agent 继续分析：对未解析集触发 script-agent（episode_start 指向首个未完成集） ---------- */
const pendingEpisodes = computed(() => episodeGrid.value.filter((e) => e.status !== 'succeeded'))
const { mutate: doContinueAgent, isPending: continuing } = useMutation({
  mutationFn: () => {
    const vid = scriptVersionId.value
    if (!vid) throw new Error('尚未保存剧本版本')
    return runScriptAgent(projectId.value, vid, scriptText.value, pendingEpisodes.value[0]?.episode_no)
  },
  onSuccess: (res) => {
    parseJobId.value = res.job_id
    void queryClient.invalidateQueries({ queryKey: ['parse-status', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
    message.success('已继续分析未解析集')
  },
  onError: (e) => message.error(errMsg(e)),
})
const candidateReady = computed(() => parseJob.value?.status === 'succeeded')

/* ---------- 停止解析 ----------
 * 后端 POST /jobs/{id}/cancel 对 script_agent 已改为协作式取消：
 * 置进程内信号，解析线程在下一个 SSE 行检查点退出，终态写 cancelled 且不落结果。
 * 前端只需把 job id 发过去，然后立即把本地 running 态清掉——
 * 不能等轮询：LLM 线程可能正卡在 socket read，最长要等到下一行数据才退出。
 */
const { mutate: doStopParse, isPending: stopping } = useMutation({
  mutationFn: () => {
    if (!parseJobId.value) throw new Error('没有正在进行的解析任务')
    return cancelJob(parseJobId.value)
  },
  onSuccess: () => {
    message.success('已停止解析，模型调用已中断')
    parseJobId.value = null
    candidateVersionId.value = null
    void queryClient.invalidateQueries({ queryKey: ['parse-status', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
  },
  onError: (e) => message.error(errMsg(e)),
})

/* ---------- 重新解析 ----------
 * 与「开始解析」的区别：force=true 跳过后端幂等复用。
 * 不加 force 时后端发现「同 script_version_id 同 episode_start 已有成功任务」
 * 会直接复用旧结果并返回 idempotent_reuse —— 用户点「重新解析」拿到的还是
 * 上一次那份有问题的结果，按钮等于没生效。
 *
 * 解析中点击时**先终止再重解析**（需求明确要求）：
 * 旧实现里按钮在 running 时被 v-if 隐藏，用户必须先点「停止解析」、等任务
 * 真的退出、再点「重新解析」，三步操作中间还要赌取消是否生效。更糟的是
 * 直接重发会在队列里堆一个新任务，而 worker 是单线程串行 —— 旧任务还在跑
 * 就烧着 token，新任务干等，用户看到的是「点了没反应」。
 * 所以这里把「取消 + 重新发起」合成一个原子动作：取消必须**等到后端确认**
 * （cancel 接口返回 204）再发新任务，否则新任务可能被刚写入的 cancelled
 * 状态影响，或与旧任务的清理竞争。
 */
const { mutate: doReparse, isPending: reparsing } = useMutation({
  mutationFn: async () => {
    const vid = scriptVersionId.value
    if (!vid) throw new Error('尚未保存剧本版本')
    // 解析中：先终止当前任务（等后端确认），再重新发起
    const runningJobId = parseJobRunning.value ? parseJobId.value : null
    if (runningJobId) {
      await cancelJob(runningJobId)
      // 本地立刻清态：cancel 已确认，running 轮询下一次会读到 cancelled
      parseJobId.value = null
    }
    return runScriptAgent(projectId.value, vid, scriptText.value, 1, true)
  },
  onSuccess: (res) => {
    parseJobId.value = res.job_id
    candidateVersionId.value = null
    void queryClient.invalidateQueries({ queryKey: ['parse-status', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
    message.success('已重新发起解析，旧结果将在新结果确认后被替换')
  },
  onError: (e) => message.error(errMsg(e)),
})

/* ---------- 刷新恢复 ---------- */

const candidateResult = computed<ParseJobResult | null>(
  () => (parseJob.value?.result as ParseJobResult | undefined) ?? null,
)
watch(() => candidateResult.value?.parse_version_id, (v) => {
  if (v && !candidateVersionId.value) candidateVersionId.value = v
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
        lines.push(`     ${prefix} ${sh.shot_purpose || sh.description || ''}`.trimEnd())
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
 * 为什么需要：确认解析后 resetAfterConfirmParse() 会清掉 parseJobId，
 * 分析窗（含原文）随之消失，刷新后也没有原文可查。这里独立取一次，只读不写 store。
 *
 * 为什么查两种 job_type：解析入口有两条真实链路 ——
 *   · `/parse` 走 worker 的 parse_executor，落 `job_type='parse'`
 *   · 「Agent 智能分析」走 script_agent，落 `job_type='script_agent'`
 * 两者 result 结构相同（scenes[] / characters[] / props[]）。此前只查 'parse'，
 * 导致 script_agent 解析的项目（如 prj_257406918e63）历史结果永远取不到，
 * 分镜分组恒为 0，与场景 shot_count 合计 26 冲突。
 */
const PARSE_JOB_TYPES = ['parse', 'script_agent'] as const
const { data: archivedParseJobs } = useQuery({
  queryKey: computed(() => ['jobs', 'parse-archive', projectId.value]),
  queryFn: async () => {
    const pages = await Promise.all(
      PARSE_JOB_TYPES.map((t) =>
        listJobs({ project_id: projectId.value, job_type: t, page: 1, page_size: 50 }),
      ),
    )
    const items = pages.flatMap((p) => p.items ?? [])
    // 跨类型按创建时间倒序，保证「最近一次成功解析」判定与单类型查询一致
    items.sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')))
    return { total: items.length, items }
  },
  enabled: computed(() => !!projectId.value),
})
/**
 * 解析原文：与结果分组必须同源（都用覆盖最完整的成功结果）。
 * 若这里仍按「最近一次」取，会出现「原文只含 3 镜、分组列出 26 镜」的新矛盾。
 * 数据源见下方 archivedParseResult。
 */
const archivedParseText = computed(() => formatResult(archivedParseResult.value))
/** 原文全文：优先当前任务结果，其次后端历史结果 */
const rawReportText = computed(() => formattedResult.value || archivedParseText.value)

/* ---------- 分镜 / 对白分组：直接取自解析结果 scenes[].shots[] ----------
 * 数据边界：解析结果是唯一带 shots 明细的来源（当前任务 result 或历史成功任务 result）。
 * 不额外请求 GET /projects/{id}/shots —— 该接口必须传 episode，多集需 N 次请求，
 * 而解析页只需要「解析出的结构」，完整分镜编辑仍由分镜图/分镜看板负责。
 */
/**
 * 历史解析结果对象（与 archivedParseText 同一查询，只读，不写 store）。
 *
 * 选取策略：不是「最近一次成功」，而是「覆盖最完整的那次成功」。
 * 为什么：解析支持分批（batch），一次任务可能只覆盖 1 集的一部分场景。
 * 实测 prj_257406918e63 最近一次成功任务 job_3d136de3f7f1 只含 1 场景 3 镜，
 * 而更早的 job_6de74a09a025 含完整 5 场景 26 镜。若按「最近」取，
 * 页面就会显示 3 个分镜，与场景 shot_count 合计 26 直接矛盾。
 *
 * 排序键：分镜总数 → 场景数 → 角色数 → 创建时间（同分时取更新的一次）。
 * 这样既保证明细完整，又在多个完整结果之间取最新。
 */
function resultWeight(j: Job): number[] {
  const r = (j.result ?? {}) as Record<string, any>
  const scenes = (r.scenes as any[] | undefined) ?? []
  const shots = scenes.reduce((n, sc) => n + ((sc?.shots as any[] | undefined)?.length ?? 0), 0)
  const chars = (r.characters as any[] | undefined)?.length ?? 0
  return [shots, scenes.length, chars]
}
const archivedParseResult = computed<Record<string, any> | null>(() => {
  const items = (archivedParseJobs.value?.items ?? []).filter(
    (j: Job) => j.status === 'succeeded' && j.result,
  )
  if (!items.length) return null
  const best = [...items].sort((a, b) => {
    const wa = resultWeight(a)
    const wb = resultWeight(b)
    for (let i = 0; i < wa.length; i += 1) {
      if (wb[i] !== wa[i]) return wb[i] - wa[i]
    }
    return String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''))
  })[0]
  return (best?.result ?? null) as Record<string, any> | null
})
/** 后端 scope 里的版本 ID：任务 result 缺失时（刷新后）仍能定位版本 */
const parseVersionIdFromScope = computed<string | null>(() => {
  const scope = agentStatusData.value?.scope
  return scope?.parse_version_id ?? null
})

/**
 * 同一 ParseVersion 下各批次的合并结果——**按集取最后一次**，不是全部拼接。
 *
 * 为什么必须合并（实测 prj_9db2e016b337「海风与夏日」，2026-11-21）：
 * script_agent 分批解析时，**每个批次是一个独立 Job**，各自只含本批集的
 * scenes/shots。旧实现 `candidateResult` 只取 `parseJob.result`（单个任务），
 * 于是页面上「场景 5」（来自 /scenes API，数据库真实值）与
 * 「分镜 11」（来自最后一个任务，只含第 2 集）两个数字不同源、互相矛盾。
 *
 * 为什么不能简单把所有任务拼起来：同一集被重解析多次时会重复累加。
 * 实测该项目第 1 集被重解析 6 次，全部拼接后「分镜」显示 37，而库里是 25。
 *
 * 正确语义与后端 `apply_parse_result` 的 upsert 一致：**同一集后完成的覆盖先完成的**。
 * 因此按 (episode_start, episode_count) 分组，每组只取 finished_at 最晚的那次，
 * 再把这些「每集最新」的结果合并成一份完整视图。
 */
const mergedParseResult = computed<Record<string, any> | null>(() => {
  const pvId = candidateResult.value?.parse_version_id
    ?? parseVersionIdFromScope.value
  if (!pvId) return candidateResult.value as Record<string, any> | null
  const items = (archivedParseJobs.value?.items ?? []).filter(
    (j: Job) => j.status === 'succeeded'
      && (j.result as Record<string, any> | undefined)?.parse_version_id === pvId,
  )
  if (!items.length) return candidateResult.value as Record<string, any> | null

  // 按「批次区间」分组，每组取 finished_at 最晚的一次（= 该区间最终生效结果）
  const byRange = new Map<string, Job>()
  for (const j of items) {
    const r = (j.result ?? {}) as Record<string, any>
    const key = `${Number(r.episode_start ?? 1)}-${Number(r.episode_count ?? 1)}`
    const cur = byRange.get(key)
    if (!cur || String(j.finished_at ?? '') > String(cur.finished_at ?? '')) {
      byRange.set(key, j)
    }
  }
  // 再按 episode_start 升序拼接各批次（不同区间内容不重叠）
  const sorted = [...byRange.values()].sort(
    (a, b) => Number((a.result as any)?.episode_start ?? 1)
      - Number((b.result as any)?.episode_start ?? 1),
  )

  const out: Record<string, any> = {
    characters: [], props: [], scenes: [], episodes: [], prop_events: [],
  }
  const seenChar = new Set<string>()
  const seenProp = new Set<string>()
  const seenEp = new Set<number>()
  const sceneIndex = new Map<string, Record<string, any>>()
  for (const j of sorted) {
    const r = (j.result ?? {}) as Record<string, any>
    for (const c of (r.characters as any[] | undefined) ?? []) {
      const name = String(c?.name ?? '').trim()
      if (name && !seenChar.has(name)) { seenChar.add(name); out.characters.push(c) }
    }
    for (const p of (r.props as any[] | undefined) ?? []) {
      const name = String(p?.name ?? '').trim()
      if (name && !seenProp.has(name)) { seenProp.add(name); out.props.push(p) }
    }
    for (const e of (r.episodes as any[] | undefined) ?? []) {
      const no = Number(e?.episode ?? 0)
      if (no && !seenEp.has(no)) { seenEp.add(no); out.episodes.push(e) }
    }
    for (const sc of (r.scenes as any[] | undefined) ?? []) {
      const title = String(sc?.title ?? '').trim()
      const ep = Number(sc?.episode ?? 0)
      // 场景键：集号 + 标题（跨集同名场景是不同场景，不能合并）
      const key = `${ep}::${title}`
      const cur = sceneIndex.get(key)
      if (cur) {
        // 同场景在多批出现：镜头按 (episode, seq) 去重后拼接
        const existing = new Set(
          ((cur.shots as any[] | undefined) ?? []).map(
            (s: any) => `${s?.episode ?? ep}::${s?.seq ?? 0}`,
          ),
        )
        for (const sh of (sc?.shots as any[] | undefined) ?? []) {
          const k = `${sh?.episode ?? ep}::${sh?.seq ?? 0}`
          if (!existing.has(k)) { existing.add(k); cur.shots.push(sh) }
        }
      } else {
        const copy = { ...sc, shots: [...((sc?.shots as any[] | undefined) ?? [])] }
        sceneIndex.set(key, copy)
        out.scenes.push(copy)
      }
    }
  }
  return out
})

/** 展示用解析结果：合并结果优先，其次历史兜底 */
const effectiveParseResult = computed<Record<string, any> | null>(
  () => mergedParseResult.value ?? archivedParseResult.value,
)

type ResultGroupKey = 'scene' | 'character' | 'prop' | 'shot' | 'dialogue'
const activeGroup = ref<ResultGroupKey>('scene')

/**
 * 当前查看的集：null = 全部集。
 *
 * 旧实现集数格是纯展示 `<div>`，没有任何点击事件；下方「解析结果」取的是
 * 项目级全量数据（scenes / characters / props 都不带集维度），
 * 于是点多集项目里的任意一集，下面显示的内容完全不变 —— 用户看到的
 * 「第 2 集」和「下方数据」之间没有任何联动关系。
 */
const activeEpisode = ref<number | null>(null)

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

/**
 * 全部集的解析分镜（未过滤）：集数格上的「N 个分镜」必须用它统计，
 * 若用过滤后的 parseShots，选中第 2 集时第 1 集的分镜数会变成 0。
 */
const allParseShots = computed<ParseShotRow[]>(() => {
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
        ep: Number(sh?.episode ?? sc?.episode ?? 0),
        sceneSeq,
        sceneTitle,
        shotNo: (sh?.shot_no ?? sh?.seq ?? '') as number | string,
        shotType: String(sh?.shot_type ?? ''),
        duration: sh?.duration == null ? null : Number(sh.duration),
        desc: String(sh?.shot_purpose ?? sh?.description ?? ''),
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

/** 当前集的分镜（activeEpisode 为 null 时即全部）：结果区分镜/台词表用它 */
const parseShots = computed<ParseShotRow[]>(() => {
  const ep = activeEpisode.value
  if (ep === null) return allParseShots.value
  return allParseShots.value.filter((s) => s.ep === ep)
})

/**
 * 每集真实分镜数：以解析明细 scenes[].shots[] 为唯一口径。
 *
 * 为什么不用后端 episode_parse_record.asset_count：
 * 该字段是「解析那一刻写下的快照」，一旦后续重新解析（或历史任务失败重试），
 * 记录行里的旧值不会随最新解析结果更新。实测 prj_257406918e63 该值为 3，
 * 而当前解析结果里是 26 个分镜（5 场景 shot_count 合计 7+4+4+5+6），
 * 页面同屏出现「26 分镜」和「第 1 集 3 个分镜」两个矛盾数字。
 * 明细取不到时（历史 result 已不可用）才回退快照值，并在 tooltip 里标注来源。
 */
const shotsPerEpisode = computed<Map<number, number>>(() => {
  const m = new Map<number, number>()
  // 必须用未过滤的 allParseShots：集数格要显示每一集自己的分镜数，
  // 用过滤后的 parseShots 会导致「选中第 2 集后第 1 集变成 0 个分镜」
  for (const s of allParseShots.value) {
    if (!s.ep) continue
    m.set(s.ep, (m.get(s.ep) ?? 0) + 1)
  }
  return m
})
/** 集卡片的真实分镜数：明细优先，无明细时回退后端快照 */
function episodeShotCount(ep: { episode_no: number; asset_count: number }): number | null {
  const fromDetail = shotsPerEpisode.value.get(ep.episode_no)
  if (fromDetail != null) return fromDetail
  return ep.asset_count || null
}

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

/**
 * 落库完成判据：实体表（场景/分镜）已写入，且没有「已成功但未落库」的集。
 *
 * 集级记录 succeeded 只说明**模型产出了结果**，实体写入是随后的独立步骤——
 * 两者之间有窗口期（实测 prj_a9058216747f：记录 12:40:41 succeeded、实体 12:40:42
 * 落库、版本 12:40:56 才确认）。只看记录就会让页面在实体还没落库时宣布
 * 「解析完成」，用户点「去分镜图」看到空白 —— 正是本 bug 的成因。
 */
const parsePersisted = computed<boolean>(() => {
  if (statusTotalEpisodes.value === 0) return false
  const scope = agentStatusData.value?.scope
  if (scope) {
    if ((scope.pending_persist_episodes?.length ?? 0) > 0) return false
    const db = scope.persisted
    return (db?.shots ?? 0) > 0 && (db?.scenes ?? 0) > 0
  }
  return false
})
/** 是否需要在解析收尾阶段轮询实体表：任务在跑，或已成功但还没落库 */
const needsPersistPoll = computed<boolean>(
  () =>
    parseJobRunning.value ||
    (statusTotalEpisodes.value > 0 && statusSummary.value.succeeded > 0 && !parsePersisted.value),
)

/* ---------- 分组展示行：落库前用解析产出、落库后切实体表（单一来源） ---------- */
interface ResultSceneRow { id: string; seq: number; title: string; desc: string; episodes: number[]; shotCount: number }
interface ResultPeopleRow { id: string; name: string; desc: string }
const sceneRows = computed<ResultSceneRow[]>(() => {
  if (parsePersisted.value) {
    return scenes.value.map((sc) => ({
      id: sc.id,
      seq: sc.seq,
      title: sc.title,
      desc: (sc as any).description ?? sc.summary ?? '',
      episodes: sc.episodes ?? [],
      shotCount: sc.shot_count ?? 0,
    }))
  }
  const r = effectiveParseResult.value
  return ((r?.scenes as any[] | undefined) ?? []).map((sc: any, i: number) => ({
    id: `preview-scene-${i}`,
    seq: Number(sc?.seq ?? i + 1),
    title: String(sc?.title ?? '未命名场景'),
    desc: String(sc?.description ?? ''),
    episodes: [Number(sc?.episode ?? 0)].filter((n) => n > 0),
    shotCount: (sc?.shots as any[] | undefined)?.length ?? 0,
  }))
})
const characterRows = computed<ResultPeopleRow[]>(() => {
  if (parsePersisted.value) {
    return characters.value.map((c) => ({ id: c.id, name: c.name, desc: c.description ?? '' }))
  }
  const r = effectiveParseResult.value
  return ((r?.characters as any[] | undefined) ?? [])
    .map((c: any, i: number) => ({ id: `preview-char-${i}`, name: String(c?.name ?? ''), desc: String(c?.description ?? '') }))
    .filter((c) => c.name)
})
const propRows = computed<ResultPeopleRow[]>(() => {
  if (parsePersisted.value) {
    return props.value.map((p) => ({ id: p.id, name: p.name, desc: p.description ?? '' }))
  }
  const r = effectiveParseResult.value
  return ((r?.props as any[] | undefined) ?? [])
    .map((p: any, i: number) => ({ id: `preview-prop-${i}`, name: String(p?.name ?? ''), desc: String(p?.description ?? '') }))
    .filter((p) => p.name)
})

/** 分组目录：数量为真实解析条数。落库前五类全部来自解析产出（同源，避免「分镜 11 但场景 0」同屏矛盾），
 *  落库后场景/角色/道具切到实体表，分镜/台词沿用解析明细。 */
const resultGroups = computed<
  Array<{ key: ResultGroupKey; label: string; count: number }>
>(() => [
  { key: 'scene', label: '场景', count: sceneRows.value.length },
  { key: 'character', label: '角色', count: characterRows.value.length },
  { key: 'prop', label: '道具', count: propRows.value.length },
  { key: 'shot', label: '分镜', count: parseShots.value.length },
  { key: 'dialogue', label: '台词', count: dialogueRows.value.length },
])
const activeGroupLabel = computed(
  () => resultGroups.value.find((g) => g.key === activeGroup.value)?.label ?? '',
)
/** 「分镜明细取不到但库里确有分镜」时的兜底说明用 */
const shotsWithoutDetail = computed(
  () => parseShots.value.length === 0 && totalShots.value > 0,
)

/* ---------- 确认候选版本 ---------- */
/**
 * 确认后清掉本地候选态：确认意味着「这份解析已是当前版本」，
 * 候选版本 ID 与跟踪中的任务 ID 都不再有意义（原 scriptFlow.resetAfterConfirmParse 的等价逻辑）。
 * 用本地 ref 而非全局 store，刷新页面也能靠 archivedParseJobs 恢复展示。
 */
function resetAfterConfirmParse() {
  parseJobId.value = null
  candidateVersionId.value = null
  gateResult.value = null
}
/**
 * 已发起过确认的版本 ID 集合：防止「确认成功 → 状态轮询仍是 current 且 pending 未刷新 →
 * 再次触发确认」形成请求风暴。
 *
 * pending_persist_episodes 由后端按「库里有无该集镜头」推出，落库成功后会变空，
 * 但 status 轮询有 4 秒间隔，这期间 confirmTarget 仍非空。用集合做一次性闩锁。
 */
const confirmedVersionIds = ref<Set<string>>(new Set())
const { mutate: doConfirmParse } = useMutation({
  mutationFn: (parseVersionId: string) =>
    confirmParseVersion(projectId.value, parseVersionId, project.value?.current_parse_version_id),
  onSuccess: (data) => {
    if (data?.gate) {
      gateResult.value = data.gate as ParseGateResult
    } else {
      gateResult.value = null
    }
    void fetchReports()
    if (gateResult.value?.needs_review) {
      message.warning(`解析已确认，但存在 ${gateResult.value.items.filter(i => i.severity === 'warning').length} 条审核提醒，请查看准入报告`)
    } else {
      message.success('解析结果已确认，场景/角色/道具已更新')
    }
    resetAfterConfirmParse()
    void queryClient.invalidateQueries({ queryKey: ['scenes', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['characters', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['props', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
  },
  onError: (e) => {
    // 幂等处理：多批次重复确认时版本已是 current（仅候选状态可确认），视为已确认成功，不弹错误
    const msg = errMsg(e)
    if (msg.includes('仅候选状态') || msg.includes('conflict')) {
      resetAfterConfirmParse()
      void queryClient.invalidateQueries({ queryKey: ['script-agent-status', projectId.value] })
      return
    }
    message.error(msg)
  },
})
/** Agent 分批解析：首批完成后 has_more=true，剩余集需用户『继续分析』推进，此时不自动确认 */
const agentHasMore = computed(
  () => (parseJob.value?.result as { has_more?: boolean } | undefined)?.has_more === true,
)

/* ---------- 场景/角色/道具 ---------- */
const { data: scenesData } = useQuery({
  queryKey: computed(() => ['scenes', projectId.value]),
  queryFn: () => listScenes(projectId.value, { page_size: 100 }),
  enabled: computed(() => !!projectId.value),
  // 解析收尾阶段轮询：实体落库后自动刷新，不再只依赖本页确认回调（另一会话/Agent
  // 确认时本页永远停在 0，正是「场景/角色/道具全 0」的直接原因）
  refetchInterval: () => (needsPersistPoll.value ? 2000 : false),
})
/** 场景（全部集，未过滤）：totalShots 与 hasResults 的口径 */
const allScenes = computed<SceneWithShots[]>(
  () => (scenesData.value?.items as SceneWithShots[] | undefined) ?? [],
)
/**
 * 当前集场景：场景是项目级、跨集复用的（同一地点多集只建一条记录，
 * 集数关系在 scene_episode 关联表 → 接口返回 episodes[]）。
 * activeEpisode 为 null 时返回全部。
 */
const scenes = computed<SceneWithShots[]>(() => {
  const ep = activeEpisode.value
  if (ep === null) return allScenes.value
  return allScenes.value.filter((sc) => (sc.episodes ?? []).includes(ep))
})
const hasResults = computed(() => allScenes.value.length > 0)
/** 解析出的镜头总数：场景接口直接带 shot_count。集数格的分镜数走 shotsPerEpisode，
 *  这里保持「全项目」口径，供「明细不可用」兜底文案使用。 */
const totalShots = computed(() => allScenes.value.reduce((n, sc) => n + (sc.shot_count ?? 0), 0))
const { data: charactersData } = useQuery({
  queryKey: computed(() => ['characters', projectId.value]),
  queryFn: () => listCharacters(projectId.value, { page_size: 100 }),
  enabled: computed(() => !!projectId.value),
  refetchInterval: () => (needsPersistPoll.value ? 2000 : false),
})
/** 角色是项目级资产（Character 表无 episode 字段），跨集共用同一批角色，
 *  因此不按集过滤 —— 按集过滤会让用户误以为「第 2 集没有角色」。 */
const characters = computed(() => charactersData.value?.items ?? [])
const { data: propsData } = useQuery({
  queryKey: computed(() => ['props', projectId.value]),
  queryFn: () => listProps(projectId.value!),
  enabled: computed(() => !!projectId.value),
  refetchInterval: () => (needsPersistPoll.value ? 2000 : false),
})
/** 道具同为项目级资产（Prop 表无 episode 字段），不按集过滤 */
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
  if (parseAllSucceeded.value && !parsePersisted.value) return { label: '正在写入结果', tone: 'running' }
  if (parseAllSucceeded.value) return { label: '解析完成', tone: 'succeeded' }
  if (statusTotalEpisodes.value > 0) return { label: '待解析', tone: 'pending' }
  return { label: '暂无结果', tone: 'pending' }
})
const pendingIssueCount = computed(() => failedEpisodes.value.length)
/**
 * 概览统计：只放分组导航**没有**的信息。
 *
 * 此前 6 项里有 4 项（场景/角色/道具/分镜/台词）与左侧分组导航的计数完全重复，
 * 同屏两处相同数字既占空间又让人怀疑哪个才是准的。
 * 现只保留：
 *   · 集        —— 分组导航不覆盖（它是「解析了几个剧本集」，不是结果类型）
 *   · 待处理    —— 失败集数量，唯一的告警级信息
 * 其余计数一律由分组导航承担（它同时也是筛选入口，数字放在那里更自然）。
 */
const overviewStats = computed(() => {
  const items: Array<{ key: string; label: string; value: number; tone?: string }> = [
    { key: 'episode', label: '集', value: statusTotalEpisodes.value },
  ]
  if (pendingIssueCount.value) {
    items.push({ key: 'issue', label: '待处理', value: pendingIssueCount.value, tone: 'issue' })
  }
  return items
})

/* ---------- 刷新恢复：从后端最近一次 script_agent 任务恢复运行态 ----------
 * 依赖 GET /projects/{id}/script-agent/status（返回最近一个 script_agent 任务的 id/status）。
 * 只恢复「仍在排队/运行」的任务：已结束的任务不接管，避免页面重新进入失败态展示，
 * 失败信息由集级解析状态 + 失败面板负责（单一事实来源）。
 * 查询本体已上移到 parsePersisted 之前声明，此处只保留恢复逻辑。
 */
watch(
  () => agentStatusData.value,
  (s) => {
    if (!s?.job) return
    const incoming = s.job as { id: string; status: string }
    const active = ['queued', 'running']
    // 后端权威：优先接管 running 任务。
    // 旧实现只在 parseJobId 为空时接管一次，之后永不更正 ——
    // 连点两次「开始解析」会锁在最新那个 queued 任务上，
    // 而真正在跑的是更早创建的 running 任务，页面于是永久显示
    // 「任务排队中」+ 已耗时停在排队任务上（实测就是这个表现）。
    if (active.includes(incoming.status)) {
      if (parseJobId.value !== incoming.id) {
        // running 一定优先于本地持有的 queued
        const curStatus = parseJob.value?.status
        if (incoming.status === 'running' || curStatus !== 'running') {
          parseJobId.value = incoming.id
        }
      }
      return
    }
    // 已结束任务只在本地还没有任务时接管（用于刷新后展示失败/成功结果）
    if (!parseJobId.value && ['failed', 'lost', 'succeeded'].includes(incoming.status)) {
      parseJobId.value = incoming.id
    }
  },
  { immediate: true },
)

/**
 * 自动确认的判定目标：以「后端事实」为准，不依赖任务态。
 *
 * 为什么不能只看 parseJob：parseJobId 是页面局部 ref，刷新或离开页面即丢，
 * 而 getScriptAgentStatus 只接管 queued/running/failed/lost（成功的任务不接管）。
 * 旧实现里 parseJobId 存在 Pinia store，离开再回来仍能触发确认；改成局部状态后
 * 「解析成功 → 场景/角色/道具落库」这一步就断了 ——
 * 实测 prj_68c1eb304355 解析出 6 个分镜，但场景/角色/道具计数全为 0。
 *
 * 因此改读 scope.parse_version_status === 'candidate'：后端已把解析版本写成候选，
 * 确认它才是让场景/角色/道具真正落库的动作。该字段刷新后依然可取，
 * 直接访问 /project/:id/parse 也能正确补齐，符合「状态由事实推导」的原则。
 */
const confirmTarget = computed<string | null>(() => {
  const scope = agentStatusData.value?.scope
  if (!scope?.parse_version_id) return null
  // 任务仍在跑时绝不确认：此时后端 job.result 还是空的，
  // apply_parse_result 拿不到场景/角色/道具，却会把版本置成 current，
  // 结果页面显示「解析完成」但场景/角色/道具全是 0（实测发生过）。
  if (parseJobRunning.value) return null
  // 分批解析中途不确认：半成品不能定成当前版本
  if (agentHasMore.value) return null
  // 还有失败集未处理时先不确认，让用户看到失败并重试
  if (statusTotalEpisodes.value > 0 && statusSummary.value.succeeded < statusTotalEpisodes.value) {
    return null
  }
  // 候选版本：正常自动确认路径
  if (scope.parse_version_status === 'candidate') return scope.parse_version_id
  // 已 current 但仍有集「解析成功却未落库」：继续批次复用了同一个 ParseVersion，
  // 而 candidate 判据已不成立 —— 旧实现到这里直接 return null，
  // 于是后续批次的镜头永远留在 job.result 里从未落库（实测 prj_9db2e016b337：
  // 第 2 集 11 个分镜在任务结果里，库里 0 条，页面还显示「第 2 集 11 个分镜」）。
  // 后端按「集级记录 succeeded 但库里查不到该集镜头」推出 pending_persist_episodes，
  // 这里据此补一次确认；确认接口对 current 版本允许 script_agent 增量落库。
  if (scope.parse_version_status === 'current' && (scope.pending_persist_episodes?.length ?? 0) > 0) {
    // 同一版本已确认过就不再重复：落库后 pending 会变空，但 4 秒轮询间隔内仍非空
    if (confirmedVersionIds.value.has(scope.parse_version_id)) return null
    return scope.parse_version_id
  }
  return null
})
watch(
  confirmTarget,
  (vid) => {
    if (vid) doConfirmParse(vid)
  },
  { immediate: true },
)
/** 何时展示分析窗：有任务在跟踪，或项目尚未解析完成（此时需要告诉用户「等待执行」） */
const analysisVisible = computed(
  () =>
    !!parseJobId.value ||
    statusTotalEpisodes.value === 0 ||
    statusSummary.value.pending === statusTotalEpisodes.value,
)
const analysisState = computed<{ label: string; tone: string }>(() => {
  if (parseJobLost.value) return { label: '状态未知', tone: 'failed' }
  if (!parseJobId.value) return { label: '等待执行', tone: 'idle' }
  if (parseJobRunning.value) return { label: '正在分析', tone: 'running' }
  const st = parseJob.value?.status
  if (st === 'succeeded') return { label: '已完成', tone: 'succeeded' }
  if (st === 'failed' || st === 'lost') return { label: '执行失败', tone: 'failed' }
  if (st === 'cancelled') return { label: '已取消', tone: 'idle' }
  return { label: '等待执行', tone: 'idle' }
})
const analysisStage = computed(() => {
  if (!parseJobId.value) return '尚未开始解析，点击右上角「开始解析」提交任务'
  if (parseJobLost.value) return '无法获取任务状态（任务可能已过期）'
  const st = parseJob.value?.status
  if (st === 'queued') return '任务排队中，等待分析引擎接管'
  if (parseJobRunning.value) {
    // 后端上报的阶段优先（大纲/评估/拆解），其次才是本地可观测事实
    if (agentStage.value?.label) return agentStage.value.label
    return llmStreamText.value ? 'AI 正在输出分析内容' : llmPlaceholder.value
  }
  if (st === 'succeeded') return `已解析 ${statusSummary.value.succeeded}/${statusTotalEpisodes.value} 集`
  if (st === 'failed') return '分析中断，失败原因见下方'
  return '任务已结束'
})
const analysisFootLabel = computed(() => {
  if (parseJobRunning.value) return '任务执行中'
  if (!parseJobId.value) return '等待任务'
  if (parseJob.value?.status === 'succeeded') return '任务已完成'
  return '任务已结束'
})

/**
 * 页头「开始解析」的可见条件：
 * - **从未成功过任何集**才叫「开始」：无集级记录，或有记录但 0 集成功（全失败/全中断）；
 * - 一旦有成功集，剩余工作由「继续分析」承担 —— 两者同时出现语义重复，
 *   用户不知道该点哪个（截图实测：0/1 集完成时两个按钮并排，其中一个是废动作）。
 * - 有任务在跑时不显示（由「停止解析」承担）。
 */
const canStartParse = computed(() => {
  if (!hasScript.value || parseJobRunning.value) return false
  if (statusTotalEpisodes.value === 0) return true
  return statusSummary.value.succeeded === 0
})

/** 页头步骤状态：与左栏 02 解析保持同一口径，但只用文字 + 色点，不重复整条进度条。
 *  完成态只写「已完成」不带集数——集数 X/Y 已在下方面板标题旁给出，
 *  页头再写一遍「1 集已完成」是同一个数字的第二次出现。 */
const parseHeaderState = computed<{ label: string; tone: string }>(() => {
  if (parseJobRunning.value) return { label: '解析中', tone: 'running' }
  if (failedEpisodes.value.length) return { label: `${failedEpisodes.value.length} 集失败`, tone: 'failed' }
  if (statusTotalEpisodes.value > 0 && statusSummary.value.succeeded === statusTotalEpisodes.value) {
    // 结果已产出但尚未落库：不能宣称「已完成」，否则用户点去分镜图看到空白
    return parsePersisted.value ? { label: '已完成', tone: 'done' } : { label: '正在写入结果', tone: 'running' }
  }
  if (!hasScript.value) return { label: '待保存剧本', tone: 'idle' }
  return { label: '待解析', tone: 'idle' }
})
/**
 * 流式输出区展示文本。
 *
 * 旧实现把 JSON 流"翻译"成一串伪进度（实测渲染出
 * `📺 正在分析第 1、1、1、1、1、1 集`），成因是用正则扫全文所有
 * `"episode": N` —— 而每个分镜都会带一次 `"episode": 1`，同一集被打印六遍。
 * 那不是进度，是统计方式错了。
 *
 * 现在的原则：**只展示真实存在的东西**。
 * - 后端 `agent.stage` 事件上报当前阶段（大纲/评估/拆解），唯一可信的阶段来源；
 * - 输出区直接呈现模型真实输出的字符流，不做任何"翻译"；
 * - 已接收字符数与已识别集号（去重后）来自流本身，可核验。
 */
const streamCharCount = computed(() => llmStreamText.value.length)
/**
 * 真实推进量：后端 job.progress 事件记录的已接收字符数（绝对值，不是百分比）。
 * 剧本解析没有可靠的「总字数」预期，编一个百分比只会误导；
 * 显示持续增长的字符数才能证明任务在往前走。
 */
const progressChars = computed(() => {
  const fromJob = parseJob.value?.progress_chars
  return Math.max(fromJob ?? 0, streamCharCount.value)
})
/** 流中已出现的集号（去重排序）——去重是关键，否则同一集会被每个分镜重复计数 */
const streamEpisodeNos = computed<number[]>(() => {
  const raw = llmStreamText.value
  if (!raw) return []
  const set = new Set<number>()
  for (const m of raw.matchAll(/"episode"\s*:\s*(\d+)/g)) {
    const n = Number(m[1])
    if (Number.isFinite(n) && n > 0) set.add(n)
  }
  return [...set].sort((a, b) => a - b)
})

/** 输出区内容：有真实流式文本就展示原文，否则给与状态相符的说明（不伪造进度） */
const analysisBodyText = computed(() => {
  if (llmStreamText.value) return llmStreamText.value
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
        <div class="title-block">
          <!-- 左侧「制作流程」栏已高亮 02 解析。解析已是独立制作节点（2026-11-20），
               页内不再有子流程胶囊；页头保留低权重步骤标识（02 + 状态）。
               H1 降为 visually-hidden 保留文档大纲与读屏语义。 -->
          <h1 class="visually-hidden">解析</h1>
          <span class="step-title">
            <span class="step-title-no">02</span>
            <span class="step-title-text">解析</span>
            <span class="step-title-state" :class="`is-${parseHeaderState.tone}`">
              {{ parseHeaderState.label }}
            </span>
          </span>
        </div>
      </div>
      <div class="parse-header-actions">
        <NButton v-if="rawReportText" quaternary @click="showRawReport = true">
          解析原文
        </NButton>
        <NButton quaternary @click="router.push(`/project/${projectId}/script`)">返回剧本</NButton>
        <!--
          停止解析：解析中常驻可见（写操作不进 ⋯ 菜单）。
          后端已改为协作式取消，点下即置进程内信号，线程在下一个 SSE 行退出；
          不依赖轮询，因此点击后立刻清本地 running 态，不等后端回执。
        -->
        <NButton
          v-if="parseJobRunning"
          type="error"
          ghost
          :loading="stopping"
          :disabled="!parseJobId"
          @click="doStopParse()"
        >
          停止解析
        </NButton>
        <!--
          继续分析：真正的「接着上次往下做」——必须**已有成功解析的集**才有意义。
          没有成功集时它和「开始解析」做的是同一件事（都从第 1 集起），
          两个按钮并排出现只会让用户不知道该点哪个（截图实测：0/1 集完成时
          两个按钮同时在场）。
        -->
        <NButton
          v-if="pendingEpisodes.length && statusSummary.succeeded > 0 && !parseJobRunning"
          :loading="continuing"
          @click="doContinueAgent()"
        >
          继续分析（{{ pendingEpisodes.length }} 集未完成）
        </NButton>
        <!--
          开始解析：本页唯一入口。原先只有「解析选中」，无分集记录时整页无法独立发起解析，
          必须回剧本页点「开始分析」再被跳回来 —— 解析独立成节点后这个回路必须断开。
          已有全部成功记录时不再出现（无意义），失败时由「重试失败集」承担。
        -->
        <NButton
          v-if="canStartParse"
          type="primary"
          :loading="starting"
          :disabled="!hasScript"
          :title="!hasScript ? '请先在剧本页保存剧本版本' : undefined"
          :aria-label="!hasScript ? '开始解析（不可用：需先保存剧本版本）' : '开始解析'"
          @click="handleStartParse()"
        >
          开始解析
        </NButton>
        <!--
          重新解析：始终常驻可见（有剧本、有集数记录时）。
          解析中也必须能点 —— 需求要求「如果是解析中的状态先终止解析然后重新解析」，
          因此这里不能用 v-if 把它藏起来（旧实现只在 !parseJobRunning 时显示，
          用户必须先手动点「停止解析」再点本按钮，两步且要赌取消是否生效）。
          按钮内部会先 await cancelJob 再发起 force 重解析，见 doReparse。
        -->
        <NButton
          v-if="hasScript && statusTotalEpisodes > 0"
          :loading="reparsing"
          :disabled="reparsing"
          @click="doReparse()"
        >
          {{ parseJobRunning ? '重新解析（将先终止当前任务）' : '重新解析' }}
        </NButton>
        <!-- 无剧本版本时的唯一出路：直接给去剧本页的入口，不让用户卡在死页 -->
        <NButton
          v-else-if="!hasScript"
          type="primary"
          @click="router.push(`/project/${projectId}/script`)"
        >
          去保存剧本 →
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
            <span v-if="streamEpisodeNos.length" class="ac-progress">
              已涉及第 {{ streamEpisodeNos.join('、') }} 集
            </span>
            <span v-if="progressChars" class="ac-progress">
              已接收 {{ (progressChars / 1000).toFixed(1) }}k 字符
            </span>
            <span class="ac-elapsed" :title="`任务开始时间：${parseJob?.started_at || parseJob?.created_at || '—'}`">
              已耗时 {{ elapsedText }}
            </span>
          </div>

          <div class="ac-divider" />

          <div class="ac-body-label">
            Agent 实时输出
            <span v-if="parseJobRunning && !llmStreamText" class="ac-waiting">等待首个 token…</span>
          </div>
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

        <!-- 集数解析状态：分析进行中不展示，分析完成后才显示。
             单集且全部成功时整块不渲染：此时「分集解析结果」里的每一项
             （1/1 集完成、第 1 集 26 个分镜）在下方「解析结果」卡里都已出现
             （解析完成徽章、1 集、分镜 26），只是同一批数字换了地方再说一遍，
             却占掉 162px（实测首屏 36%）。多集时它才是新信息（哪集失败、哪集待解析）。 -->
        <div
          v-if="statusTotalEpisodes && !parseJobRunning && !(statusTotalEpisodes === 1 && parseAllSucceeded)"
          class="parse-status-panel panel-card"
        >
          <div class="episode-toolbar">
            <span class="pane-label">分集解析结果</span>
            <span class="episode-count-tag">{{ statusSummary.succeeded }}/{{ statusTotalEpisodes || 0 }} 集完成</span>
            <span v-if="submittedEpsLabel" class="episode-count-tag is-selected">本次解析 {{ submittedEpsLabel }}</span>
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
          <!-- 集格与状态图例同排：集数少时图例靠右补位，避免右侧整片留白。
               图例只解释「集格颜色」这一件事：全部成功时集格全是绿底，
               再列一遍「待解析 0 / 进行中 0 / 已完成 1 / 失败 0」是同一批数字的
               第三次出现（标题旁已有「1/1 集完成」），因此整条不渲染；
               有非成功集时才出现，且只列计数 > 0 的档位。 -->
          <div class="episode-row">
            <div class="episode-grid">
              <!-- 全部：多集项目的默认视图，也是唯一能同时看到跨集场景的入口 -->
              <button
                v-if="episodeGrid.length > 1"
                type="button"
                class="episode-cell is-all"
                :class="{ 'is-active': activeEpisode === null }"
                :aria-pressed="activeEpisode === null"
                @click="activeEpisode = null"
              >
                <span class="episode-cell-no">全部</span>
                <span class="episode-cell-meta">{{ statusTotalEpisodes }} 集</span>
              </button>
              <button
                v-for="ep in visibleEpisodes"
                :key="ep.episode_no"
                type="button"
                class="episode-cell"
                :class="[`is-${ep.status}`, { 'is-active': activeEpisode === ep.episode_no }]"
                :aria-pressed="activeEpisode === ep.episode_no"
                :title="`第 ${ep.episode_no} 集：${statusLabel(ep.status)}${episodeShotCount(ep) ? '，解析出 ' + episodeShotCount(ep) + ' 个分镜' : ''}${ep.error_msg ? '，' + ep.error_msg : ''}（点击查看该集结果）`"
                @click="activeEpisode = activeEpisode === ep.episode_no ? null : ep.episode_no"
              >
                <span class="episode-cell-no">第 {{ ep.episode_no }} 集</span>
                <span class="episode-cell-meta" :class="ep.status !== 'succeeded' ? ['status-tag', `is-${ep.status}`] : undefined">{{ ep.status === 'succeeded' ? (episodeShotCount(ep) ?? 0) + ' 个分镜' : statusLabel(ep.status) }}</span>
              </button>
            </div>
            <div v-if="hasMixedEpisodeStatus" class="episode-legend">
              <span v-if="statusSummary.pending" class="legend-item"><i class="dot is-pending" />待解析 {{ statusSummary.pending }}</span>
              <span v-if="statusSummary.running" class="legend-item"><i class="dot is-running" />进行中 {{ statusSummary.running }}</span>
              <span v-if="statusSummary.succeeded" class="legend-item"><i class="dot is-succeeded" />已完成 {{ statusSummary.succeeded }}</span>
              <span v-if="statusSummary.failed" class="legend-item"><i class="dot is-failed" />失败 {{ statusSummary.failed }}</span>
            </div>
          </div>
        </div>

        <!-- 解析结果：状态 + 紧凑概览 + 分组内容，统计只在这里出现一次 -->
        <section v-if="hasResults || parseAllSucceeded" class="result-panel">
          <header class="result-head">
            <h3 class="result-title">解析结果</h3>
            <span class="result-state" :class="`is-${resultState.tone}`">{{ resultState.label }}</span>
            <!-- 当前筛选的集：结果区所有计数都限定在这个范围，必须显式标注，
                 否则用户会以为「场景 4」是全项目数字 -->
            <span v-if="activeEpisode !== null" class="result-scope">
              第 {{ activeEpisode }} 集
              <button type="button" class="result-scope-clear" title="查看全部集" @click="activeEpisode = null">×</button>
            </span>
            <span class="result-spacer" />
            <NButton
              size="tiny"
              quaternary
              :disabled="!parsePersisted"
              :title="parsePersisted ? '前往分镜图' : '解析结果正在写入，写入完成后可用'"
              @click="router.push(`/project/${projectId}/storyboard-images`)"
            >
              去分镜图 →
            </NButton>
            <NButton
              v-if="parseAllSucceeded"
              size="small"
              type="primary"
              :disabled="!parsePersisted"
              :title="parsePersisted ? '前往资产库' : '解析结果正在写入，写入完成后可用'"
              @click="router.push(`/project/${projectId}/assets`)"
            >
              去资产库 →
            </NButton>
          </header>

          <!-- 概览条：只承载「集」与「待处理」——分组导航没覆盖的两项。
               场景/角色/道具/分镜/台词的计数统一在下方分组导航里，不重复展示。 -->
          <div class="overview-stats">
            <div
              v-for="s in overviewStats"
              :key="s.key"
              class="ov-item"
              :class="{ 'is-issue': s.tone === 'issue' }"
            >
              <span class="ov-value">{{ s.value }}</span>
              <span class="ov-label">{{ s.label }}</span>
            </div>
          </div>

          <!-- P1-9：三份独立报告卡片 -->
          <div v-if="reportsLoading && !scriptReports" class="report-loading">
            <span class="spinner"></span> 报告数据加载中...
          </div>
          <div v-if="scriptReports" class="report-cards">
            <div class="report-card">
              <div class="report-header">
                <span class="report-title">📝 剧本质量</span>
                <span class="report-status" :class="reportStatusClass(scriptReports.quality_report.status)">
                  {{ reportStatusLabel(scriptReports.quality_report.status) }}
                </span>
              </div>
              <div class="report-summary">{{ scriptReports.quality_report.summary }}</div>
              <div v-if="scriptReports.quality_report.status !== 'skipped'" class="report-metrics">
                <span>评分 {{ scriptReports.quality_report.items.overall_score }}/10</span>
                <span>问题 {{ scriptReports.quality_report.items.issues_count }} 个</span>
                <span v-if="scriptReports.quality_report.items.severity.critical" class="metric-critical">严重 {{ scriptReports.quality_report.items.severity.critical }}</span>
              </div>
            </div>
            <div class="report-card">
              <div class="report-header">
                <span class="report-title">🔍 解析完整性</span>
                <span class="report-status" :class="reportStatusClass(scriptReports.integrity_report.status)">
                  {{ reportStatusLabel(scriptReports.integrity_report.status) }}
                </span>
              </div>
              <div class="report-summary">{{ scriptReports.integrity_report.summary }}</div>
              <div v-if="scriptReports.integrity_report.status !== 'skipped'" class="report-metrics">
                <span v-if="scriptReports.integrity_report.items.completion_status" :class="scriptReports.integrity_report.items.completion_status === 'complete' ? 'metric-ok' : 'metric-warn'">
                  完成度 {{ scriptReports.integrity_report.items.completion_status }}
                </span>
                <span v-if="scriptReports.integrity_report.items.parsed_episodes != null">
                  解析 {{ scriptReports.integrity_report.items.parsed_episodes }}/{{ scriptReports.integrity_report.items.expected_episodes ?? '?' }} 集
                </span>
                <span v-if="scriptReports.integrity_report.items.successful_batches != null">
                  成功批次 {{ scriptReports.integrity_report.items.successful_batches }}
                </span>
                <span v-if="scriptReports.integrity_report.items.failed_batches" class="metric-critical">失败批次 {{ scriptReports.integrity_report.items.failed_batches }}</span>
                <span v-if="scriptReports.integrity_report.items.coverage?.dialogues">
                  对白匹配 {{ scriptReports.integrity_report.items.coverage.dialogues.matched }}/{{ scriptReports.integrity_report.items.coverage.dialogues.source }}
                </span>
              </div>
            </div>
            <div class="report-card">
              <div class="report-header">
                <span class="report-title">🎬 制作可行性</span>
                <span class="report-status" :class="reportStatusClass(scriptReports.feasibility_report.status)">
                  {{ reportStatusLabel(scriptReports.feasibility_report.status) }}
                </span>
              </div>
              <div class="report-summary">{{ scriptReports.feasibility_report.summary }}</div>
              <div v-if="scriptReports.feasibility_report.items.total_alerts" class="report-metrics">
                <span :class="scriptReports.feasibility_report.items.total_alerts > 5 ? 'metric-critical' : ''">
                  风险项 {{ scriptReports.feasibility_report.items.total_alerts }} 条
                </span>
              </div>
            </div>
          </div>

          <!-- 生产准入报告：确认解析后若存在 NEES_REVIEW 等级则展示 -->
          <div v-if="gateResult?.needs_review" class="gate-report">
            <div class="gate-header">
              <span class="gate-badge" :class="`is-${gateResult.level.toLowerCase()}`">
                {{ gateResult.level === 'NEEDS_REVIEW' ? '需审核' : gateResult.level }}
              </span>
              <span class="gate-summary">
                确认已完成，但存在 {{ gateResult.items.filter(i => i.severity !== 'info').length }} 条需关注的准入项
              </span>
            </div>
            <ul class="gate-items">
              <li
                v-for="(item, idx) in gateResult.items.filter(i => i.severity !== 'info')"
                :key="idx"
                class="gate-item"
                :class="`sev-${item.severity}`"
              >
                <span class="gate-item-tag">{{ item.severity === 'blocker' ? '阻塞' : item.severity === 'warning' ? '警告' : '' }}</span>
                <span class="gate-item-msg">{{ item.message }}</span>
              </li>
            </ul>
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
                <span class="group-count">{{ g.count }}</span>
              </button>
            </nav>

            <div class="group-pane" role="region" :aria-label="activeGroupLabel">
              <template v-if="activeGroup === 'scene'">
                <ol v-if="sceneRows.length" class="result-scenes is-tall">
                  <li v-for="sc in sceneRows" :key="sc.id">
                    <span class="rs-seq">#{{ sc.seq }}</span>
                    <span class="rs-body">
                      <span class="rs-title" :title="sc.title">{{ sc.title }}</span>
                      <span v-if="sc.desc" class="rs-desc" :title="sc.desc">{{ sc.desc }}</span>
                    </span>
                    <span v-if="sc.episodes?.length" class="rs-ep">第 {{ sc.episodes.join('、') }} 集</span>
                    <span class="rs-count">{{ sc.shotCount }} 镜</span>
                  </li>
                </ol>
                <p v-else class="pane-empty">未解析出场景</p>
              </template>

              <template v-else-if="activeGroup === 'character'">
                <ul v-if="characterRows.length" class="result-people is-tall">
                  <li v-for="c in characterRows" :key="c.id">
                    <span class="rp-name">{{ c.name }}</span>
                    <span v-if="c.desc" class="rp-desc" :title="c.desc">{{ c.desc }}</span>
                  </li>
                </ul>
                <p v-else class="pane-empty">未解析出角色</p>
              </template>

              <template v-else-if="activeGroup === 'prop'">
                <ul v-if="propRows.length" class="result-people is-tall">
                  <li v-for="pr in propRows" :key="pr.id">
                    <span class="rp-name">{{ pr.name }}</span>
                    <span v-if="pr.desc" class="rp-desc" :title="pr.desc">{{ pr.desc }}</span>
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
                  <NButton
                    size="tiny"
                    quaternary
                    :disabled="!parsePersisted"
                    :title="parsePersisted ? '前往分镜图' : '解析结果正在写入，写入完成后可用'"
                    @click="router.push(`/project/${projectId}/storyboard-images`)"
                  >
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
            </div>
          </div>
        </section>

        <!-- 空态：说清「现在什么情况」，动作由页头那唯一一个主按钮承担 ——
             同屏两个同名主按钮会让用户犹豫点哪个（PageHeader 的约定是每页一个主操作）。

             v-else-if="!parseJobRunning"：任务运行中**不渲染**这张卡。
             旧实现无条件渲染，于是同一屏出现「正在分析」+「解析尚未产出结果」
             两句互相否定的文案，用户以为任务已经失败了。运行中的状态由上方
             AI Agent 卡独家承载，这里只在「没有任务在跑」时补充说明。
             文案也只指向本页真实渲染出来的区块（分集面板在运行中并不渲染）。 -->
        <div v-else-if="!parseJobRunning" class="parse-empty">
          <p class="pe-title">
            {{ !hasScript ? '还没有可解析的剧本' : statusTotalEpisodes === 0 ? '剧本已就绪，等待解析' : '解析尚未产出结果' }}
          </p>
          <p class="pe-desc">
            {{
              !hasScript
                ? '解析以已保存的剧本版本为准。先到剧本页写入正文并保存版本，再回到这里开始解析。'
                : statusTotalEpisodes === 0
                  ? '解析会把已保存的剧本拆成场景、角色、道具与分镜。集数由 Agent 自行判断，无需先选集。'
                  : `已提交 ${statusTotalEpisodes} 集，其中 ${statusSummary.succeeded} 集完成、${statusSummary.failed} 集失败。${statusSummary.failed > 0 ? '失败原因见上方分集面板中该集的错误说明。' : '任务已结束，点「开始解析」可重新发起。'}`
            }}
          </p>
        </div>
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
  --parse-col-max: 1080px;
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

/* 步骤标识：02 + 名称 + 状态。视觉权重明显低于左栏 active 项（左栏有品牌底 + 反色编号），
   只承担「这是第几步、现在什么状态」，不承担「我在哪一页」。 */
.title-block { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.step-title { display: inline-flex; align-items: baseline; gap: 8px; min-width: 0; }
.step-title-no {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.step-title-text {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}
.step-title-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-tertiary);
  white-space: nowrap;
}
.step-title-state::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--neutral-line);
  flex-shrink: 0;
}
.step-title-state.is-running {
  color: var(--brand-hover);
}
.step-title-state.is-running::before {
  background: var(--brand-primary);
  animation: step-dot-pulse 1.6s ease-in-out infinite;
}
.step-title-state.is-done {
  color: var(--state-success);
}
.step-title-state.is-done::before {
  background: var(--state-success);
}
.step-title-state.is-failed {
  color: var(--danger-text);
}
.step-title-state.is-failed::before {
  background: var(--brand-error);
}
@keyframes step-dot-pulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 var(--brand-line);
  }
  50% {
    box-shadow: 0 0 0 3px var(--brand-soft);
  }
}
@media (prefers-reduced-motion: reduce) {
  .step-title-state.is-running::before {
    animation: none;
  }
}

/* 空态：不是 NEmpty 的图标 + 一行字，而是「说清现在什么情况 + 下一步做什么」 */
.parse-empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-3);
  padding: var(--sp-8) var(--sp-5);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
}
.pe-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.pe-desc {
  margin: 0;
  max-width: 62ch;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.parse-header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.parse-body { display: flex; flex: 1; min-height: 0; min-width: 0; }
.content-pane { flex: 1; min-width: 0; padding: 24px 20px 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; }
/* 内容列限宽居中：避免超宽屏下卡片内部左右两端（标题 / 操作按钮）被拉开过远。
   限宽加在卡片自身而非 .content-pane 上，防止 flex:1 + margin:auto 把内容压成窄柱。
   1080 是实测取的值：1366 视口下内容列从 960 → 1080，右侧死白由 190px 降到 70px。 */
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
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
/* 「等待首个 token」：解析启动到首个 delta 之间有几十秒空窗，
   没有这句提示，空输出框会被当成卡死。 */
.ac-waiting { color: var(--brand-hover); }
.ac-body {
  /* 输出区高度：解析输出会持续几十秒到几分钟，必须自己滚动。
     这里用视口相对上限（而不是 flex:1）—— 卡片不是 flex 容器，
     flex 在此不生效；上一版写 max-height:none 会让内容无限增高、
     滚动条消失，用户看到的是「不可滚动」。 */
  max-height: min(46vh, 460px);
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--sp-2) var(--sp-4) var(--sp-3);
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
  /* 滚动条常驻可见：内容超出时用户要知道「下面还有」 */
  scrollbar-width: thin;
  scrollbar-color: var(--neutral-line) transparent;
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
/* 集数格现在是筛选按钮：必须让用户看出「可点」与「当前选中」。
   选中态用 2px 主色描边 + 外发光，避免与「成功集」的绿底混淆（两者可叠加）。 */
.episode-cell { cursor: pointer; font: inherit; }
.episode-cell:focus-visible { outline: 2px solid var(--brand-primary); outline-offset: 2px; }
.episode-cell.is-active { border-color: var(--brand-primary); box-shadow: 0 0 0 2px var(--brand-soft); }
.episode-cell.is-all { background: var(--bg-panel); color: var(--text-secondary); }
.episode-cell.is-all.is-active { background: var(--brand-soft); color: var(--brand-primary); }
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
/* 当前集筛选标记：结果区计数已限定到该集，需要与状态标签区分开（状态是结论，这个是范围） */
.result-scope {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 4px 2px 8px;
  border-radius: var(--radius);
  border: 1px solid var(--brand-line);
  background: var(--brand-soft);
  color: var(--brand-primary);
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.result-scope-clear {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
  border-radius: 3px;
}
.result-scope-clear:hover { background: var(--brand-line); }

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
.group-item.is-active .group-count { color: var(--brand-hover); }
.group-pane { min-width: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.pane-empty { margin: 0; padding: var(--sp-4) 0; font-size: 12.5px; color: var(--text-tertiary); }
.pane-foot { display: flex; justify-content: flex-end; }
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
