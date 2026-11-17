<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpin, NEmpty, NTag,
  NCheckbox, NModal, NRadioGroup, NRadio, NSelect, NProgress, useMessage,
} from 'naive-ui'
import { listCharacters, listProps, listScenes, listSceneShots, reorderSceneShots } from '@/api/script'
import { generateProjectShots } from '@/api/storyboard'
import { listJobs } from '@/api/jobs'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { listWorkflows } from '@/api/workflows'
import { getClipGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { Scene, Shot, ShotKind } from '@/types/script'
import type { ClipGenerationJob, ClipGenerationProgress } from '@/types/generation'
import ShotDetailModal from '@/components/storyboard/ShotDetailModal.vue'
import type { ClipTier } from '@/components/storyboard/useShotDetail'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- 视频生成档位：极速 5s / 标准 5s / 质量 10s（对应库里三套 4 步工作流） ---
const clipTier = ref<ClipTier>('std_5s')
const clipTierOptions = [
  { label: '极速 5s（最快，480×864）', value: 'fast_5s' },
  { label: '标准 5s（推荐，640×1120）', value: 'std_5s' },
  { label: '质量 10s（最清晰，768×1344）', value: 'quality_10s' },
]
const clipWorkflowsQuery = useQuery({
  queryKey: ['workflows', 'video'],
  queryFn: () => listWorkflows({ workflow_type: 'video', page_size: 50 }),
  staleTime: 5 * 60 * 1000,
})
/** 当前档位对应的 workflow_id；库里没有时返回 undefined，后端按默认工作流匹配 */
const clipWorkflowId = computed(() => {
  // /api/workflows 返回的是数组（非分页对象），这里两种形态都兼容
  const data: any = clipWorkflowsQuery.data.value
  const items: Array<{ id: string; name: string }> = Array.isArray(data) ? data : (data?.items ?? [])
  return items.find((w) => w.name.endsWith(clipTier.value))?.id
})

// --- Data fetching ---
const scenesQuery = useQuery({
  queryKey: ['scenes', projectId],
  queryFn: () => listScenes(projectId.value, { include_shots: false }),
})

// Per-scene shots cache
const shotsMap = ref<Record<string, Shot[]>>({})
const loadingShots = ref<Record<string, boolean>>({})

async function loadSceneShots(sceneId: string, episode: number) {
  loadingShots.value[sceneId] = true
  try {
    const res = await listSceneShots(sceneId, { episode })
    shotsMap.value[sceneId] = res.items
  } catch (e: any) {
    message.error(`加载镜头失败: ${e?.message ?? e}`)
  } finally {
    loadingShots.value[sceneId] = false
  }
}



// --- Drag & drop (same scene only) ---
const dragShotId = ref<string | null>(null)
const dragSceneId = ref<string | null>(null)

function onDragStart(e: DragEvent, shot: Shot, sceneId: string) {
  dragShotId.value = shot.id
  dragSceneId.value = sceneId
  e.dataTransfer?.setData('text/plain', shot.id)
}

function onDragOver(e: DragEvent, sceneId: string) {
  if (dragSceneId.value === sceneId) {
    e.preventDefault()
  }
}

function onDrop(e: DragEvent, targetShot: Shot, sceneId: string) {
  e.preventDefault()
  if (dragSceneId.value !== sceneId || !dragShotId.value) return
  const shots = shotsMap.value[sceneId]
  if (!shots) return
  const fromIdx = shots.findIndex(s => s.id === dragShotId.value)
  const toIdx = shots.findIndex(s => s.id === targetShot.id)
  if (fromIdx === -1 || toIdx === -1 || fromIdx === toIdx) return
  const reordered = [...shots]
  const [moved] = reordered.splice(fromIdx, 1)
  reordered.splice(toIdx, 0, moved)
  shotsMap.value[sceneId] = reordered
  const reorderEpisode = reordered[0]?.episode
  if (reorderEpisode === undefined) {
    dragShotId.value = null
    dragSceneId.value = null
    return
  }
  reorderSceneShots(sceneId, reordered.map(s => s.id), reorderEpisode).catch((e: any) => {
    message.error(`排序失败: ${e?.message ?? e}`)
    loadSceneShots(sceneId, reorderEpisode)
  })
  dragShotId.value = null
  dragSceneId.value = null
}

// --- 镜头详情（左预览 + 右三标签页：提示词 / 参考图 / 生成） ---
const detailVisible = ref(false)
const detailShotId = ref<string | null>(null)

function openShotDetail(shot: Shot) {
  detailShotId.value = shot.id
  detailVisible.value = true
}

/** 详情当前镜头：按有序镜头列表解析，列表刷新后自动跟随，不额外保存副本 */
const activeShot = computed(() => orderedShots.value.find((s) => s.id === detailShotId.value) ?? null)
const detailStatus = computed(() => (activeShot.value ? getShotStatus(activeShot.value) : null))
/** 该镜头是否有在途视频任务：决定主按钮是「生成视频」还是「重新生成」 */
const detailClipJobActive = computed(
  () => !!detailShotId.value && activeClipJobByShot.value.has(detailShotId.value),
)

// --- 分镜视频任务状态：轮询在途 generate_clip 任务，视频未产出前不显示已完成 ---
// 轮询间隔：存在在途任务时每 20s 刷新一次进度，无在途任务时停止轮询
const clipJobsQuery = useQuery({
  queryKey: ['jobs', projectId, 'generate_clip'],
  queryFn: () => listJobs({ project_id: projectId.value, job_type: 'generate_clip', page_size: 100 }),
  refetchInterval: (query) => {
    const items = (query.state.data as { items?: Array<{ status: string }> } | undefined)?.items ?? []
    const hasActive = items.some(
      (j) =>
        j.status === 'queued' ||
        j.status === 'running' ||
        j.status === 'chained_wait' ||
        j.status === 'submission_unknown'
    )
    return hasActive ? 20000 : false
  },
})
const CLIP_STATUS_RANK: Record<string, number> = {
  running: 3,
  submission_unknown: 3,
  queued: 2,
  chained_wait: 1,
}
const activeClipJobByShot = computed(() => {
  // 同一镜头可能同时有多个在途任务（例如「重新生成」时旧任务在跑、批量又排了一条）：
  // 优先级 running > queued > chained_wait，同级取创建时间最新的，避免"正在生成"被新排队任务盖掉
  const best = new Map<string, { rank: number; created: string; status: string }>()
  for (const j of clipJobsQuery.data.value?.items ?? []) {
    if (!j.shot_id) continue
    const rank = CLIP_STATUS_RANK[j.status]
    if (!rank) continue
    const created = j.created_at ?? ''
    const cur = best.get(j.shot_id)
    if (!cur || rank > cur.rank || (rank === cur.rank && created > cur.created)) {
      best.set(j.shot_id, { rank, created, status: j.status })
    }
  }
  const map = new Map<string, 'queued' | 'running' | 'chained_wait' | 'submission_unknown'>()
  for (const [shotId, item] of best) {
    map.set(shotId, item.status as 'queued' | 'running' | 'chained_wait' | 'submission_unknown')
  }
  return map
})
// 镜头生成进度百分比：取该镜头在途 generate_clip 任务的 progress（0-100）
const clipProgressByShot = computed(() => {
  const map = new Map<string, number>()
  const latestCreatedByShot = new Map<string, string>()
  for (const j of clipJobsQuery.data.value?.items ?? []) {
    if (j.shot_id && j.status === 'running' && typeof j.progress === 'number') {
      const created = j.created_at ?? ''
      const prev = latestCreatedByShot.get(j.shot_id)
      if (prev === undefined || created > prev) {
        latestCreatedByShot.set(j.shot_id, created)
        map.set(j.shot_id, j.progress)
      }
    }
  }
  return map
})

// --- 视频生成总体进度（后端聚合：X/Y、正在跑哪一镜、队列名次与预计等待） ---
const clipProgress = useGenerationProgress<ClipGenerationProgress>({
  fetcher: () => getClipGenerationProgress(projectId.value),
  // 视频单镜耗时长，3s 轮询足够；有 chained_wait 时同样保持轮询，队列名次才会实时前移
  onChange: () => {
    // 完成一镜后刷新当前集镜头，让缩略图/状态跟上
    for (const scene of filteredScenes.value) {
      const ep = (scene.episodes ?? [])[0]
      if (ep !== undefined) void loadSceneShots(scene.id, ep)
    }
  },
  onAllComplete: () => {
    message.success('全部分镜视频已生成完成，可进入合成与导出')
  },
})

/** 在途视频任务 → 镜头（同镜多任务时 running 优先） */
const inflightClipByShot = computed(() => {
  const map = new Map<string, ClipGenerationJob>()
  for (const job of clipProgress.data.value?.inflight ?? []) {
    if (!job.shot_id) continue
    const cur = map.get(job.shot_id)
    if (!cur || (cur.status !== 'running' && job.status === 'running')) map.set(job.shot_id, job)
  }
  return map
})

/** 最近失败镜头（按镜头去重） */
const failedClipByShot = computed(() => {
  const map = new Map<string, string>()
  for (const f of clipProgress.data.value?.failed_recent ?? []) {
    if (f.shot_id && !map.has(f.shot_id)) map.set(f.shot_id, f.error_msg || '未知原因')
  }
  return map
})

/** 正在执行的镜头（running）与排队镜头数 */
const runningClipJob = computed(
  () => (clipProgress.data.value?.inflight ?? []).find((j) => j.status === 'running') ?? null,
)
const queuedClipCount = computed(() => {
  const p = clipProgress.data.value
  return (p?.inflight_queued ?? 0) + (p?.inflight_chained_wait ?? 0)
})

/** 剩余时间文案：把秒数转成「约 X 分」 */
const estimatedWaitText = computed(() => {
  const sec = clipProgress.data.value?.estimated_wait_seconds
  if (!sec || sec <= 0) return ''
  if (sec < 60) return '约 1 分钟内'
  const min = Math.round(sec / 60)
  return min >= 60 ? `约 ${Math.floor(min / 60)} 小时 ${min % 60} 分` : `约 ${min} 分`
})

const clipsDoneCount = computed(() => clipProgress.data.value?.with_clip ?? allShots.value.filter((s) => !!s.current_assets?.clip).length)

const clipHeadline = computed(() => {
  const p = clipProgress.data.value
  const total = allShots.value.length || p?.total || 0
  const done = clipsDoneCount.value
  const running = runningClipJob.value
  if (running) {
    const pct = typeof running.progress === 'number' ? ` ${running.progress}%` : ''
    const scene = running.scene_title ? `${running.scene_title} · ` : ''
    return `正在生成视频：${scene}镜 ${running.shot_seq ?? '—'}${pct}`
  }
  if (p?.inflight_count) return `已排队 ${p.inflight_count} 个视频任务，等待开始渲染`
  if (total > 0 && done === total) return '全部分镜视频已生成完成'
  if (!total) return '暂无镜头'
  return `还有 ${total - done} 个镜头未生成视频`
})

/** 单镜在途状态文案：生成中 42% / 排队第 3 位 / 等待前序镜头 */
function clipInflightText(job?: ClipGenerationJob): string {
  if (!job) return ''
  if (job.status === 'running') return typeof job.progress === 'number' ? `视频生成中 ${job.progress}%` : '视频生成中'
  if (job.status === 'chained_wait') return job.queue_rank > 0 ? `等待前序镜头（第 ${job.queue_rank} 位）` : '等待前序镜头'
  if (job.status === 'queued') return job.queue_rank > 0 ? `排队第 ${job.queue_rank} 位` : '排队中'
  return '提交确认中'
}

/** 批量出视频/重新生成后：立即建基线并轮询，全部完成后统一提示 */
async function trackClipProgress() {
  await clipProgress.start({ notifyOnComplete: true })
}

// 页面进入/刷新：有在途视频任务才轮询（放在 allShots 等依赖定义之后，避免初始化顺序问题）
void clipProgress.bootstrap()

// --- Status badge ---
type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'
interface StatusInfo { label: string; color: string; kind: StatusKind; icon?: string }
function getShotStatus(shot: Shot): StatusInfo {
  const ca = shot.current_assets
  // 正在生成优先：重新生成时镜头已有旧视频，也要显示"视频生成中"而不是"已完成"
  const progressJob = inflightClipByShot.value.get(shot.id)
  const jobStatus = progressJob?.status ?? activeClipJobByShot.value.get(shot.id)
  if (jobStatus === 'running' || jobStatus === 'submission_unknown') {
    const pct = progressJob?.progress ?? clipProgressByShot.value.get(shot.id)
    return {
      label: typeof pct === 'number' ? `视频生成中 ${pct}%` : '视频生成中',
      color: 'var(--brand-primary)',
      kind: 'running',
    }
  }

  const has = ca && (ca.keyframe || ca.clip || ca.audio)
  if (!has) {
    return { label: '未生成', color: 'var(--text-tertiary)', kind: 'stale' }
  }
  // 已有视频：排队的"重新生成"不影响展示，仍以已有产物为准（排队信息保留在任务列表里）
  if (ca?.clip) {
    const con = shot.consistency
    if (con) {
      if (con.verdict === 'passed') return { label: '已完成', color: 'var(--state-success)', kind: 'succeeded' }
      if (con.verdict === 'review') return { label: '待审核', color: 'var(--state-warning)', kind: 'pending' }
      if (con.verdict === 'failed') return { label: '待重做', color: 'var(--state-warning)', kind: 'pending' }
    }
    return { label: '已完成', color: 'var(--state-success)', kind: 'succeeded' }
  }

  // 还没有视频：显示排队/等前序（带队列名次），失败优先提示重试，否则待生成
  if (failedClipByShot.value.has(shot.id)) {
    return { label: '视频生成失败', color: 'var(--state-danger)', kind: 'failed' }
  }
  if (jobStatus === 'queued') {
    const rank = progressJob?.queue_rank
    return { label: rank ? `排队第 ${rank} 位` : '视频排队中', color: 'var(--text-secondary)', kind: 'pending' }
  }
  if (jobStatus === 'chained_wait') {
    const rank = progressJob?.queue_rank
    return { label: rank ? `等待前序（第 ${rank} 位）` : '等待前序镜头完成', color: 'var(--text-secondary)', kind: 'pending' }
  }
  return { label: '待生成视频', color: 'var(--text-tertiary)', kind: 'stale' }
}

const shotKindLabels: Record<ShotKind, string> = {
  dialogue: '对白',
  narration: '旁白',
  visual: '画面',
  transition: '转场',
}

// --- 集选择器（集级资产工作台：按集切换） ---
const allEpisodes = computed(() => {
  const eps = new Set<number>()
  for (const sc of scenesQuery.data.value?.items ?? []) {
    for (const ep of sc.episodes ?? []) eps.add(ep)
  }
  return Array.from(eps).sort((a, b) => a - b)
})
const activeEpisode = ref<number | null>(null)
watch(allEpisodes, (eps) => {
  if (eps.length && (activeEpisode.value === null || !eps.includes(activeEpisode.value))) {
    activeEpisode.value = eps[0]
  }
}, { immediate: true })
const filteredScenes = computed(() => {
  const items = scenesQuery.data.value?.items ?? []
  const ep = activeEpisode.value
  if (ep === null) return items
  return items.filter((s) => (s.episodes ?? []).includes(ep))
})
// --- 全片连续镜头序号 ---
// shot.seq 是场景内序号，各场景都会从 1 开始，直接展示会出现「镜头1 镜头2 镜头3」多组重复。
// 这里按场景 seq 升序 + 场景内镜头 seq 升序累加，得到当前集内的全局镜号（1..N）。
const orderedShots = computed<Shot[]>(() => {
  const scenes = [...filteredScenes.value].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
  const list: Shot[] = []
  for (const sc of scenes) {
    const shots = [...(shotsMap.value[sc.id] ?? [])].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
    for (const s of shots) list.push(s)
  }
  return list
})
const globalSeqMap = computed(() => {
  const map = new Map<string, number>()
  orderedShots.value.forEach((s, i) => map.set(s.id, i + 1))
  return map
})
/** 全片镜号：取不到时回落到场景内序号 */
const shotGlobalSeq = (shot: Shot) => globalSeqMap.value.get(shot.id) ?? shot.seq
// 场景数据或所选集数变化时按集加载镜头（episode 为后端必填参数，缺失会返回 422）
watch(
  [() => scenesQuery.data.value, activeEpisode],
  ([data, ep]) => {
    shotsMap.value = {}
    if (!data?.items) return
    data.items.forEach((s: Scene) => {
      const eps = s.episodes ?? []
      const target = ep !== null && eps.includes(ep) ? ep : eps[0]
      if (target !== undefined) loadSceneShots(s.id, target)
    })
  },
  { immediate: true }
)
// --- 公共资产齐全度门禁（未齐全引导回资产库） ---
const { data: charsData } = useQuery({
  queryKey: ['characters', projectId],
  queryFn: () => listCharacters(projectId.value, { page_size: 100 }),
})
const { data: propsData } = useQuery({
  queryKey: ['props', projectId],
  queryFn: () => listProps(projectId.value),
})
const readiness = computed(() => {
  const cs = (charsData.value?.items ?? []) as Array<{ portrait_asset_id?: string | null }>
  const ss = (scenesQuery.data.value?.items ?? []) as Array<{ baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ anchor_asset_id?: string | null }>
  const total = cs.length + ss.length + ps.length
  const done =
    cs.filter((c) => !!c.portrait_asset_id).length +
    ss.filter((s) => !!s.baseline_asset_id).length +
    ps.filter((p) => !!p.anchor_asset_id).length
  return { total, done, all: total > 0 && done === total }
})

/** 详情里保存提示词后：后端会做失效传播，需要重新拉取该镜头的产物状态 */
async function handlePromptsSaved(shot: Shot) {
  queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  await loadSceneShots(shot.scene_id, shot.episode)
}

/** 详情里提交生成任务后：刷新列表与任务，并启动进度轮询 */
function handleDetailSubmitted() {
  queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  queryClient.invalidateQueries({ queryKey: ['jobs', projectId, 'generate_clip'] })
  void trackClipProgress()
}

// --- 出口：全部镜头视频生成完成后可去合成导出 ---
const allShots = computed(() => Object.values(shotsMap.value).flat())
const clipsDone = computed(() => {
  const shots = allShots.value
  return shots.length > 0 && shots.every((s) => !!s.current_assets?.clip)
})

// --- 批量出视频：仅提交已确认分镜图的镜头（D-13 闸口），未确认的跳过并提示 ---
// --- 勾选批量：勾选多个/全选镜头后仅对勾选项提交 ---
const selectedShotIds = ref<string[]>([])
function toggleShot(id: string, checked: boolean) {
  const set = new Set(selectedShotIds.value)
  if (checked) set.add(id)
  else set.delete(id)
  selectedShotIds.value = [...set]
}
const allShotsSelected = computed(() =>
  allShots.value.length > 0 && allShots.value.every((s) => selectedShotIds.value.includes(s.id)),
)
function toggleSelectAllShots(checked: boolean) {
  selectedShotIds.value = checked ? allShots.value.map((s) => s.id) : []
}
const batchClipMutation = useMutation({
  mutationFn: (payload: { ids: string[]; chainMode: 'independent' | 'chained' }) =>
    generateProjectShots(projectId.value, { shot_ids: payload.ids, target: 'clip', chain_mode: payload.chainMode, workflow_id: clipWorkflowId.value }),
  onSuccess: async (res: any) => {
    const skipped = (res?.skipped || []).filter((s: any) => s.reason === 'already_generated')
    if ((res?.count ?? 0) > 0) {
      message.success(`已提交 ${res.count} 个分镜视频任务，正在生成（进度见顶部进度面板）`)
    }
    if (skipped.length) {
      message.info(`${skipped.length} 个镜头已由该工作流生成过视频，已跳过；如需重做请在分镜视频详情页重新生成`)
    }
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
    queryClient.invalidateQueries({ queryKey: ['jobs', projectId, 'generate_clip'] })
    await trackClipProgress()
  },
  onError: (e: any) => {
    // 幂等：全部镜头都已生成过视频时不是故障，提示用户走详情页重新生成
    if (e?.code === 'already_generated') {
      message.info(e?.message ?? '选中镜头均已生成过视频，如需重做请在分镜视频详情页重新生成')
      return
    }
    message.error(`批量出视频失败: ${e?.message ?? e}`)
  },
})

function handleBatchClip() {
  const shots = allShots.value
  if (!shots.length) {
    message.warning('当前集暂无镜头')
    return
  }
  const picked = shots.filter((s) => selectedShotIds.value.includes(s.id))
  if (!picked.length) {
    message.warning('请先勾选要出视频的镜头')
    return
  }
  const confirmed = picked.filter((s) => s.keyframe_confirmed && !!s.current_assets?.keyframe)
  const pending = picked.length - confirmed.length
  if (!confirmed.length) {
    message.warning(`勾选的 ${picked.length} 个镜头的分镜图均未确认，请先到分镜图环节逐镜确认`)
    return
  }
  if (pending > 0) {
    message.info(`本次提交 ${confirmed.length} 个已确认镜头，${pending} 个未确认分镜图的镜头已跳过`)
  }
  // 弹窗选择批量生成方式：独立并行（不依赖上一镜尾帧）/ 链式串行（逐镜衔接）
  pendingClipShots.value = confirmed
  showChainModeModal.value = true
}

// --- 批量出视频方式选择弹窗 ---
const showChainModeModal = ref(false)
const chainMode = ref<'independent' | 'chained'>('chained')
const pendingClipShots = ref<Shot[]>([])
function confirmChainMode() {
  showChainModeModal.value = false
  void submitClipsByPlans(pendingClipShots.value, chainMode.value)
}

/** 按已定稿的视频定义批量提交：名称与关联以库中定义为准；定义缺失时回退镜头级批量 */
async function submitClipsByPlans(picked: Shot[], mode: 'independent' | 'chained') {
  try {
    const res = await listAssetPlans(projectId.value, { plan_type: 'clip' })
    const pickedIds = new Set(picked.map((s) => s.id))
    const planIds = (res.items || [])
      .filter((p) => p.owner_id && pickedIds.has(p.owner_id))
      .map((p) => p.id)
    if (!planIds.length) {
      batchClipMutation.mutate({ ids: picked.map((s) => s.id), chainMode: mode })
      return
    }
    const gen = await generateFromPlans(projectId.value, { plan_type: 'clip', plan_ids: planIds, chain_mode: mode, workflow_id: clipWorkflowId.value })
    message.success(`已按定义提交 ${gen.count} 个分镜视频任务，完成后请刷新查看`)
    const skipped = (gen.skipped || []).filter((s) => s.reason !== 'no_pending_plans')
    if (skipped.length) {
      const doneCount = skipped.filter((s) => s.reason === 'already_generated').length
      const others = skipped.filter((s) => s.reason !== 'already_generated')
      if (doneCount) {
        message.info(`${doneCount} 条定义已由该工作流生成过视频，已跳过；如需重做请在分镜视频详情页重新生成`)
      }
      if (others.length) {
        const reasons = [...new Set(others.map((s) => s.reason))].join('、')
        message.info(`另有 ${others.length} 条定义被跳过：${reasons}`)
      }
    }
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
    queryClient.invalidateQueries({ queryKey: ['jobs', projectId, 'generate_clip'] })
  } catch (e: any) {
    // 幂等：定义对应的视频均已生成过，属于预期跳过而非失败
    if (e?.code === 'no_generatable_plans' || e?.code === 'already_generated') {
      message.info('所选定义对应的视频均已生成过，已全部跳过；如需重做请在分镜视频详情页重新生成')
      return
    }
    message.error(`批量出视频失败: ${e?.message ?? e}`)
  }
}
</script>

<template>
  <div class="storyboard-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">分镜看板</h1>
      </div>
    </header>
    <!-- 集选择器 -->
    <div v-if="allEpisodes.length" class="episode-bar">
      <span class="episode-label">集数</span>
      <button
        v-for="ep in allEpisodes"
        :key="ep"
        class="episode-chip"
        :class="{ active: activeEpisode === ep }"
        @click="activeEpisode = ep"
      >第 {{ ep }} 集</button>
    </div>
    <!-- 视频生成总体进度：X/Y、正在跑哪一镜、队列名次与预计等待、是否全部完成 -->
    <div
      class="gen-panel"
      :class="{
        done: clipsDoneCount > 0 && clipsDoneCount === (allShots.length || clipProgress.data.value?.total || 0) && !clipProgress.hasInflight.value,
        fail: failedClipByShot.size > 0,
      }"
    >
      <div class="gen-head">
        <span class="gen-badge">{{ clipProgress.hasInflight.value ? '⏳' : clipsDoneCount > 0 && clipsDoneCount === (allShots.length || 0) ? '✓' : '•' }}</span>
        <span class="gen-title">{{ clipHeadline }}</span>
        <span class="gen-nums">
          视频 {{ clipsDoneCount }}/{{ allShots.length || clipProgress.data.value?.total || 0 }}（{{ clipProgress.data.value?.percent ?? 0 }}%）
        </span>
        <n-button size="tiny" quaternary :loading="clipProgress.loading.value" @click="clipProgress.refresh()">刷新进度</n-button>
      </div>
      <n-progress
        type="line"
        :percentage="clipProgress.data.value?.percent ?? 0"
        :height="8"
        :border-radius="4"
        :show-indicator="false"
        :color="clipProgress.data.value?.all_done ? 'var(--state-success)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle)"
        class="gen-bar"
      />
      <div class="gen-sub">
        <span v-if="clipProgress.data.value?.inflight_running" class="gen-state running">
          渲染中 {{ clipProgress.data.value.inflight_running }}
        </span>
        <span v-if="queuedClipCount" class="gen-state queued">
          排队/等待前序 {{ queuedClipCount }}
        </span>
        <span v-if="estimatedWaitText" class="gen-state">预计剩余 {{ estimatedWaitText }}</span>
        <span v-if="clipProgress.data.value?.all_done" class="gen-state ok">
          全部分镜视频已生成完成<template v-if="clipProgress.data.value?.last_completed_at">（最近完成于 {{ new Date(clipProgress.data.value.last_completed_at).toLocaleTimeString('zh-CN', { hour12: false }) }}）</template>
        </span>
        <span v-else-if="allShots.length" class="gen-state">未生成 {{ allShots.length - clipsDoneCount }}</span>
        <span v-if="failedClipByShot.size" class="gen-state fail">
          最近失败 {{ failedClipByShot.size }} 个
        </span>
        <span v-if="clipProgress.error.value" class="gen-state fail">{{ clipProgress.error.value }}</span>
      </div>
      <!-- 在途明细：正在渲染的那一镜 + 接下来排队的几镜 -->
      <div v-if="clipProgress.data.value?.inflight?.length" class="gen-inflight">
        <span
          v-for="job in clipProgress.data.value.inflight.slice(0, 6)"
          :key="job.job_id"
          class="gen-inflight-item"
        >
          <span class="gen-inflight-name">
            {{ job.scene_title || '未知场景' }} · 镜 {{ job.shot_seq ?? '—' }}
          </span>
          <span class="gen-inflight-state" :class="job.status">{{ clipInflightText(job) }}</span>
        </span>
        <span v-if="clipProgress.data.value.inflight.length > 6" class="gen-inflight-more">
          等 {{ clipProgress.data.value.inflight.length }} 个任务
        </span>
      </div>
    </div>
    <!-- 公共资产齐全度门禁横幅 -->
    <div class="gate-banner" :class="{ ok: readiness.all }">
      <span class="gate-badge">{{ readiness.all ? '✓' : '⏳' }}</span>
      <span class="status-tag" :class="readiness.all ? 'is-succeeded' : 'is-pending'">
        {{ readiness.all ? '资产已齐全' : '资产未齐全' }}
      </span>
      <span class="gate-text">
        {{ readiness.all ? '公共资产已齐全，可生成分镜图与分镜视频' : `公共资产未齐全：${readiness.done}/${readiness.total}，生成分镜图前请先在资产库完成生成` }}
      </span>
      <n-button size="small" quaternary @click="router.push(`/project/${projectId}/assets`)">去资产库</n-button>
      <n-checkbox
        :checked="allShotsSelected"
        :disabled="!allShots.length"
        @update:checked="toggleSelectAllShots"
      >全选</n-checkbox>
      <n-select
        v-model:value="clipTier"
        :options="clipTierOptions"
        size="small"
        style="width: 210px"
        title="批量出视频使用的档位（极速/标准/质量）"
      />
      <n-button
        size="medium"
        type="primary"
        :disabled="!readiness.all || !selectedShotIds.length"
        :loading="batchClipMutation.isPending.value"
        @click="handleBatchClip"
      >批量出视频（{{ selectedShotIds.length }}）</n-button>
      <n-button size="small" type="primary" :disabled="!clipsDone" @click="router.push(`/project/${projectId}/compose`)">去合成导出</n-button>
    </div>
    <!-- Loading -->
    <n-spin v-if="scenesQuery.isLoading.value" class="page-loading" />

    <!-- Error -->
    <div v-else-if="scenesQuery.isError.value" class="state-block">
      <p class="state-text">场景加载失败：{{ scenesQuery.error.value instanceof Error ? scenesQuery.error.value.message : '请稍后重试' }}</p>
      <n-button secondary @click="() => scenesQuery.refetch()">重试</n-button>
    </div>

    <!-- Empty -->
    <n-empty v-else-if="scenesQuery.data.value?.items?.length === 0" description="暂无场景，请先完成剧本解析">
      <template #extra>
        <n-button type="primary" @click="router.push(`/project/${projectId}/parse`)">前往解析页</n-button>
      </template>
    </n-empty>

    <!-- Scenes -->
    <div v-else class="scenes-container">
      <div v-for="scene in filteredScenes" :key="scene.id" class="scene-group">
        <div class="scene-header">
          <span class="scene-seq">#{{ scene.seq }}</span>
          <span class="scene-title">{{ scene.title }}</span>
          <n-tag v-if="scene.episodes?.length" size="tiny" round>
            第 {{ scene.episodes.join(', ') }} 集
          </n-tag>
          <span class="shot-count">{{ scene.shot_count ?? 0 }} 镜头</span>
        </div>

        <div
          class="shots-row"
          @dragover="onDragOver($event, scene.id)"
        >
          <n-spin v-if="loadingShots[scene.id]" size="small" />
          <template v-else>
            <div
              v-for="shot in shotsMap[scene.id] || []"
              :key="shot.id"
              class="shot-card-wrapper"
              draggable="true"
              @dragstart="onDragStart($event, shot, scene.id)"
              @drop="onDrop($event, shot, scene.id)"
              @dragover.prevent
            >
              <div class="shot-card" @click="openShotDetail(shot)">
                <n-checkbox
                  class="shot-check"
                  :checked="selectedShotIds.includes(shot.id)"
                  @update:checked="(v: boolean) => toggleShot(shot.id, v)"
                  @click.stop
                />
                <div class="shot-thumb">
                  <img
                    v-if="shot.current_assets?.keyframe?.thumb_url || shot.current_assets?.keyframe?.url"
                    :src="shot.current_assets.keyframe!.thumb_url || shot.current_assets.keyframe!.url"
                    alt="thumbnail"
                  />
                  <div v-else class="thumb-placeholder">
                    <span>{{ shotKindLabels[shot.shot_kind] }}</span>
                  </div>
                  <div class="status-badge" :style="{ background: getShotStatus(shot).color }">
                    {{ getShotStatus(shot).label }}
                  </div>
                </div>
                <div class="shot-info">
                  <div class="shot-title">{{ shot.title || `镜头 ${shotGlobalSeq(shot)}` }}</div>
                  <div class="shot-meta">
                    <span>{{ shot.duration }}s</span>
                    <span>第{{ shot.episode }}集</span>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 镜头详情：左预览 + 右三标签页（提示词 / 参考图 / 生成），支持 ←/→/Enter/Esc/R -->
    <ShotDetailModal
      v-model:show="detailVisible"
      v-model:shot-id="detailShotId"
      v-model:clip-tier="clipTier"
      :shots="orderedShots"
      :project-id="projectId"
      :readiness-all="readiness.all"
      :status-kind="detailStatus?.kind ?? 'stale'"
      :status-label="detailStatus?.label ?? '未知状态'"
      :kind-label="activeShot ? shotKindLabels[activeShot.shot_kind] : ''"
      :clip-job-active="detailClipJobActive"
      :clip-workflow-id="clipWorkflowId"
      :clip-tier-options="clipTierOptions"
      :scenes="scenesQuery.data.value?.items ?? []"
      :characters="charsData?.items ?? []"
      :prop-items="propsData?.items ?? []"
      @submitted="handleDetailSubmitted"
      @prompts-saved="handlePromptsSaved"
    />
    <!-- 批量出视频方式选择弹窗：独立并行（不依赖上一镜尾帧）/ 链式串行（逐镜衔接） -->
    <n-modal
      v-model:show="showChainModeModal"
      preset="dialog"
      title="选择批量生成方式"
      positive-text="开始生成"
      negative-text="取消"
      @positive-click="confirmChainMode"
    >
      <div style="margin: 12px 0 4px">
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px">
          <span style="font-size: 13px; color: var(--text-secondary)">视频档位</span>
          <n-select
            v-model:value="clipTier"
            :options="clipTierOptions"
            size="small"
            style="width: 210px"
          />
        </div>
        <n-radio-group v-model:value="chainMode">
          <div style="display: flex; flex-direction: column; gap: 12px">
            <n-radio value="independent">
              独立生成（并行）
              <div style="font-size: 12px; color: var(--text-tertiary); margin-top: 2px">
                各镜头互不依赖，不使用上一镜尾帧，多 ComfyUI 实例可并行生成，速度快
              </div>
            </n-radio>
            <n-radio value="chained">
              链式生成（串行）
              <div style="font-size: 12px; color: var(--text-tertiary); margin-top: 2px">
                按镜头顺序逐镜生成，下一镜首帧取上一镜实际尾帧，画面衔接更连贯，耗时较长
              </div>
            </n-radio>
          </div>
        </n-radio-group>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.storyboard-page {
  padding: var(--sp-4);
  min-height: calc(100vh - var(--app-header-height, 56px));
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
.episode-bar { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; margin-bottom: var(--sp-3); }
.episode-label { font-size: 13px; color: var(--text-secondary); }
.episode-chip { border: 1px solid var(--border-subtle); background: var(--bg-panel); border-radius: var(--radius); padding: 6px var(--sp-4); font-size: 14px; color: var(--text-secondary); cursor: pointer; transition: border-color var(--transition-fast), color var(--transition-fast); }
.episode-chip:hover { border-color: var(--brand-primary); color: var(--brand-primary); }
.episode-chip.active { background: var(--brand-soft); border-color: var(--brand-primary); color: var(--brand-primary); font-weight: 600; }
.gate-banner { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: var(--sp-3) var(--sp-4); margin-bottom: var(--sp-3); background: var(--bg-panel); }
.gate-banner.ok { border-color: var(--success-line); background: var(--success-soft); }
.gate-badge { font-size: 14px; }
.gate-text { font-size: 13px; color: var(--text-primary); margin-right: auto; }

/* ===== 视频生成总体进度面板 ===== */
.gen-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-3);
  background: var(--bg-panel);
}
.gen-panel.done { border-color: var(--success-line); background: var(--success-soft); }
.gen-panel.fail { border-color: var(--danger-line); }
.gen-head { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.gen-badge { font-size: 14px; }
.gen-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.gen-nums { font-size: 12px; color: var(--text-secondary); margin-left: auto; }
.gen-bar { width: 100%; }
.gen-sub { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; font-size: 12px; }
.gen-state { color: var(--text-secondary); }
.gen-state.queued { color: var(--state-warning); }
.gen-state.running { color: var(--brand-primary); }
.gen-state.ok { color: var(--state-success); font-weight: 500; }
.gen-state.fail { color: var(--state-danger); }
.gen-inflight { display: flex; gap: var(--sp-1); flex-wrap: wrap; }
.gen-inflight-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 2px var(--sp-2);
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
}
.gen-inflight-name { color: var(--text-primary); }
.gen-inflight-state { color: var(--text-secondary); }
.gen-inflight-state.running { color: var(--brand-primary); }
.gen-inflight-state.chained_wait { color: var(--accent-amber); }
.gen-inflight-more { font-size: 11px; color: var(--text-tertiary); align-self: center; }
.page-loading {
  display: flex;
  justify-content: center;
  padding: var(--sp-16) 0;
}

/* ===== 场景与镜头卡片 ===== */
.scenes-container {
  display: flex;
  flex-direction: column;
  gap: var(--sp-6);
}
.scene-group {
  background: transparent;
}
.scene-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.scene-seq {
  font-size: 14px;
  color: var(--brand-primary);
  font-weight: 600;
}
.scene-title {
  font-size: 16px;
  color: var(--text-primary);
  font-weight: 500;
}
.shot-count {
  font-size: 12px;
  color: var(--text-secondary);
  margin-left: auto;
}
.shots-row {
  display: flex;
  gap: var(--sp-3);
  overflow-x: auto;
  padding-bottom: var(--sp-2);
}
.shot-card-wrapper {
  position: relative;
  flex-shrink: 0;
}
.shot-card {
  width: 220px;
  height: 320px;
  background: var(--bg-panel);
  border-radius: var(--radius-xl);
  overflow: hidden;
  cursor: pointer;
  border: 1px solid var(--border-subtle);
  transition: border-color var(--transition-base);
  position: relative;
}
.shot-card:hover {
  border-color: var(--brand-primary);
}
.shot-check {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 2;
  background: var(--overlay-active);
  border-radius: var(--radius);
  padding: 2px;
}
.shot-thumb {
  width: 100%;
  height: 200px;
  position: relative;
  background: var(--bg-elevated);
}
.shot-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.thumb-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-tertiary);
  font-size: 14px;
}
.status-badge {
  position: absolute;
  bottom: 6px;
  right: 6px;
  padding: 2px var(--sp-2);
  border-radius: var(--radius-sm);
  font-size: 11px;
  color: var(--text-on-brand);
  white-space: nowrap;
}
.shot-info {
  padding: var(--sp-2) var(--sp-3);
}
.shot-title {
  font-size: 13px;
  color: var(--text-primary);
  margin-bottom: var(--sp-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.shot-meta {
  display: flex;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--text-secondary);
}

/* ===== 窄屏适配 ===== */
@media (max-width: 768px) {
  .storyboard-page { padding: var(--sp-3); }
  .scenes-container { gap: var(--sp-4); }
  .shots-row {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    overflow-x: visible;
  }
  .shot-card-wrapper { width: 100%; }
  .shot-card { width: 100%; height: auto; }
  .shot-thumb { height: 150px; }
  .shot-info { padding: 6px var(--sp-2); }
}
@media (max-width: 480px) {
  .shots-row { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); }
}
</style>
