<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpin, NEmpty, NTag,
  NCheckbox, NModal, NRadioGroup, NRadio, NProgress, useMessage,
} from 'naive-ui'
import { listCharacters, listProps, listScenes, listSceneShots, reorderSceneShots } from '@/api/script'
import { getProject } from '@/api/projects'
import { generateProjectShots } from '@/api/storyboard'
import { listJobs, cancelProjectJobs } from '@/api/jobs'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { getClipGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { Scene, Shot, ShotKind } from '@/types/script'
import { buildShotImpactMap, IMPACT_SHOT_LABEL, getWorkflowImpact, WORKFLOW_IMPACT_LABEL } from '@/api/dependencies'
import type { DependencyChangeStatus, DependencyProjectImpactPreview, WorkflowImpactItem } from '@/api/dependencies'
import type { ClipGenerationJob, ClipGenerationProgress } from '@/types/generation'
import ShotDetailModal from '@/components/storyboard/ShotDetailModal.vue'
import StoryboardPlanImportDialog from '@/components/storyboard/StoryboardPlanImportDialog.vue'
import StageCard from '@/components/ui/StageCard.vue'
import DependencyImpactOverview from '@/components/ui/DependencyImpactOverview.vue'
import WorkflowSelector from '@/components/workflow/WorkflowSelector.vue'
import StageTransitionDialog from '@/components/project/StageTransitionDialog.vue'
import { useStageTransition } from '@/composables/useStageTransition'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- 分镜视频工作流（video 槽位） ---
// 候选由后端 /workflows/candidates 按真实能力过滤：输出为视频、能接参考图，
// 且计算图里注册了 H3 多参考图节点（multi_reference_to_video）。
// 下拉主文本是工作流管理页维护的 description，不在前端硬编码档位文案；
// 未选择时自动落到默认工作流，批量 / 单个 / 重新生成 / 失败重试共用这一份选择。
const clipWorkflowId = ref<string | undefined>(undefined)

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



// --- 分镜规划导入（Phase 2）：校验只读，写入为显式安全 upsert ---
const planDialogVisible = ref(false)

/** 规划写入后刷新场景与当前集镜头，保持看板与库内一致 */
async function refreshStoryboard() {
  await scenesQuery.refetch()
  for (const scene of filteredScenes.value) {
    const ep = (scene.episodes ?? [])[0]
    if (ep !== undefined) await loadSceneShots(scene.id, ep)
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

/** 排队镜头数（排队 + 等待前序） */
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


/** 批量出视频/重新生成后：立即建基线并轮询，全部完成后统一提示 */
async function trackClipProgress() {
  await clipProgress.start({ notifyOnComplete: true })
}

/**
 * 批量取消在途出视频任务（2026-11-21）：
 * 链式批量会把后续镜头压成 chained_wait 排队，任务中心下线后没有任何入口叫停；
 * 选错工作流只能等 8 镜全跑完（每镜可能 20 分钟）。这里按 stage 批量取消。
 */
const cancellingClip = ref(false)
const confirmDialog = useConfirmDialog()
const clipInflightCount = computed(() => clipProgress.data.value?.inflight_count ?? 0)
function handleCancelClip() {
  const n = clipInflightCount.value
  if (!n) return
  const q = queuedClipCount.value
  const running = clipProgress.data.value?.inflight_running ?? 0
  confirmDialog({
    title: '取消进行中的出视频任务',
    message: `将取消当前 ${n} 个在途分镜视频任务（渲染中 ${running} 个、排队/等待前序 ${q} 个）。正在渲染的会同时中断 ComfyUI，已生成完成的视频不受影响。`,
    impact: [`在途视频任务 ${n} 个（渲染中 ${running}、排队 ${q}）`],
    confirmText: '取消这些任务',
    cancelText: '继续渲染',
    onConfirm: async () => {
      cancellingClip.value = true
      try {
        const res = await cancelProjectJobs(projectId.value, { target: 'clip' })
        message.success(
          `已取消 ${res.jobs} 个视频任务${res.interrupted ? `（中断渲染 ${res.interrupted} 个）` : ''}`,
        )
        await clipProgress.refresh()
      } catch (e: any) {
        message.error(`取消失败: ${e?.message ?? e}`)
      } finally {
        cancellingClip.value = false
      }
    },
  })
}

// 页面进入/刷新：有在途视频任务才轮询（放在 allShots 等依赖定义之后，避免初始化顺序问题）
void clipProgress.bootstrap()

// --- Status badge ---
// 注意：不含 'locked'——分镜看板的锁定功能已整体移除（2026-11 流程简化）。
// 共享组件 StatusTag 仍保留 'locked'（DirectorConsoleView 用它表示「未选择会话」），
// 但本页不产生该状态，故不纳入本页类型。
type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'stale'
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
      // 注意：这是**视频一致性检测**结果（consistency.verdict），不是已取消的
      // 阶段人工审核。文案用「待复核」而非「待审核」，避免与流程简化前的
      // 审核闸口混淆——该状态不阻断任何阶段跳转，只是提示画面可能有瑕疵。
      if (con.verdict === 'review') return { label: '待复核', color: 'var(--state-warning)', kind: 'pending' }
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
/**
 * 项目画幅 → 缩略图 aspect-ratio。
 * 9:16 竖屏项目的分镜图是竖构图，若缩略图窗口按近方形裁切，
 * 人物头脚会被 cover 掉，用户看不到真实构图。
 * 用 CSS 变量注入而非逐卡算样式，保证所有卡片同一比例。
 */
const { data: projectData } = useQuery({
  queryKey: ['project', projectId],
  queryFn: () => getProject(projectId.value),
})
const shotThumbRatio = computed(() => {
  const map: Record<string, string> = { '9:16': '9 / 16', '16:9': '16 / 9', '1:1': '1 / 1' }
  return map[projectData.value?.aspect_ratio ?? ''] ?? '4 / 5'
})
/**
 * 卡片宽度：与画幅配合，让整卡高度稳定在 ~360px 左右。
 * 竖屏 168px → 缩略图 299px；横屏 320px → 缩略图 180px；方形 240px → 缩略图 240px。
 * 目的是「同屏可见张数」在不同画幅项目间尽量接近，而不是让卡片高矮差一倍。
 */
const shotCardWidth = computed(() => {
  const map: Record<string, string> = { '9:16': '168px', '16:9': '320px', '1:1': '240px' }
  return map[projectData.value?.aspect_ratio ?? ''] ?? '200px'
})

/* ---------- 分镜行横向溢出检测 ----------
 * 目的：7 镜的场景在 1366 宽下只能看到 6 张，没有任何提示时用户会以为场景只有 6 镜。
 * 右侧渐隐 mask 在近黑背景上不可见（淡出成透明仍是黑色），所以改用显式提示按钮。
 * 检测在挂载 / 尺寸变化 / 数据变化时各跑一次，滚动时实时更新「是否还能继续右滑」。
 */
const rowRefs = ref<HTMLElement[]>([])
/** 监听行容器尺寸变化（卡片增删、画幅切换都会改变宽度） */
let rowObserver: ResizeObserver | null = null
/** sceneId → 是否还有未滚动到的内容（false 表示已滑到底，提示应隐藏） */
const rowOverflow = ref<Record<string, boolean>>({})

function measureRow(el: HTMLElement): { scene: string; more: boolean } | null {
  const scene = el.dataset.scene
  if (!scene) return null
  // 留 2px 容差：亚像素取整会让 scrollWidth 比 clientWidth 大 1
  return { scene, more: el.scrollWidth - el.scrollLeft - el.clientWidth > 2 }
}

function refreshOverflow() {
  const next: Record<string, boolean> = {}
  for (const el of rowRefs.value) {
    const m = measureRow(el)
    if (m) next[m.scene] = m.more
  }
  rowOverflow.value = next
}

function onRowScroll(e: Event, sceneId: string) {
  const el = e.target as HTMLElement
  const m = measureRow(el)
  if (!m) return
  // 只更新这一行，避免每次滚动都重算所有场景
  if (rowOverflow.value[sceneId] !== m.more) {
    rowOverflow.value = { ...rowOverflow.value, [sceneId]: m.more }
  }
}

function scrollRowRight(sceneId: string) {
  const el = rowRefs.value.find((r) => r.dataset.scene === sceneId)
  if (!el) return
  const remaining = el.scrollWidth - el.clientWidth - el.scrollLeft
  // 一次滚过约 3 张卡；剩余量不足时直接滚到底。
  // 实测「固定滚 3 卡」会停在距底 10px 处，提示按钮不消失（判定条件是 >2px）。
  const cardW = el.querySelector<HTMLElement>('.shot-card-wrapper')?.offsetWidth ?? 180
  const step = Math.min((cardW + 12) * 3, remaining)
  el.scrollBy({ left: step, behavior: 'smooth' })
}

// 数据/视口变化后重新测量（卡片数量或宽度变了，溢出状态也会变）。
// 必须 deep: true —— shotsMap 是「先赋空对象、再逐场景塞 key」的写法，
// 只改属性不改引用，浅层 watch 永远不触发（实测过：按钮一直不出现）。
watch(
  [() => shotsMap.value, shotCardWidth, shotThumbRatio],
  () => nextTick(refreshOverflow),
  { deep: true },
)
onMounted(() => {
  nextTick(refreshOverflow)
  window.addEventListener('resize', refreshOverflow)
  // 卡片图片加载会改变行高但不改宽度，ResizeObserver 兜住容器尺寸变化
  if (typeof ResizeObserver !== 'undefined') {
    rowObserver = new ResizeObserver(() => refreshOverflow())
    nextTick(() => rowRefs.value.forEach((el) => rowObserver?.observe(el)))
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', refreshOverflow)
  rowObserver?.disconnect()
  rowObserver = null
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

// --- 依赖影响 → 镜头卡片常驻标记（Phase 3 / 7，只读） ---
// 卡三「检查依赖影响」手动触发后，把待处理清单派生为 shotId → 状态映射，
// 受影响镜头在缩略图左上角常驻警示角标，直到下次检查结果更新或刷新页面。
// 保持与后端只读策略一致：只提示、不推断依赖、不提供自动重做入口。
const shotImpactByShotId = ref<Map<string, DependencyChangeStatus>>(new Map())
function handleImpactLoaded(data: DependencyProjectImpactPreview) {
  shotImpactByShotId.value = buildShotImpactMap(data)
}
function shotImpactLabel(shotId: string): string {
  const status = shotImpactByShotId.value.get(shotId)
  return (status && IMPACT_SHOT_LABEL[status]) || ''
}

// --- 工作流版本影响（Phase 3 / 6.5，只读） ---
// 已生成任务使用的工作流版本落后于当前版本（或工作流已停用）时，
// 在镜头缩略图左下角提示；只提示、不自动重做，重做仍走既有生成入口。
const workflowImpactQuery = useQuery({
  queryKey: ['workflow-impact', projectId],
  queryFn: () => getWorkflowImpact(projectId.value),
})
const workflowImpactByShot = computed(() => {
  const map = new Map<string, WorkflowImpactItem>()
  for (const item of workflowImpactQuery.data.value?.items ?? []) {
    if (!item.shot_id) continue
    // 同镜头多条任务时：停用优先于版本变化，便于优先处理不可再生成的镜头
    const cur = map.get(item.shot_id)
    if (!cur || (cur.change_status !== 'workflow_disabled' && item.change_status === 'workflow_disabled')) {
      map.set(item.shot_id, item)
    }
  }
  return map
})
function workflowImpactBadgeText(shotId: string): string {
  const item = workflowImpactByShot.value.get(shotId)
  if (!item) return ''
  return item.change_status === 'workflow_disabled' ? '工作流已停用' : '工作流已更新'
}
function workflowImpactLabel(shotId: string): string {
  const item = workflowImpactByShot.value.get(shotId)
  if (!item) return ''
  const name = item.workflow_name ?? item.workflow_id
  const current = item.current_workflow_version == null ? '—' : `v${item.current_workflow_version}`
  return `${name}：v${item.job_workflow_version} → ${current}（${WORKFLOW_IMPACT_LABEL[item.change_status]}）`
}

// --- 按依赖状态筛选镜头（Phase 3 / 7，纯前端只读） ---
// 数据只来自卡三最近一次「检查依赖影响」，没有检查结果时不显示筛选条，
// 也不在本地推断依赖；筛选只影响看板显示，不改选中集合与任何后端状态。
const impactFilter = ref<'all' | 'affected'>('all')
const impactFilterActive = computed(() => shotImpactByShotId.value.size > 0)
const affectedShotCount = computed(
  () => allShots.value.filter((s) => shotImpactByShotId.value.has(s.id)).length,
)
const displayScenes = computed(() => {
  if (impactFilter.value === 'all') return filteredScenes.value
  return filteredScenes.value.filter((scene) => {
    if (loadingShots.value[scene.id]) return true
    return (shotsMap.value[scene.id] || []).some((s) => shotImpactByShotId.value.has(s.id))
  })
})
function visibleShots(sceneId: string): Shot[] {
  const shots = shotsMap.value[sceneId] || []
  if (impactFilter.value === 'all') return shots
  return shots.filter((s) => shotImpactByShotId.value.has(s.id))
}
// 新一轮检查若已无受影响镜头，自动回到「全部镜头」，避免看板空白被误读为没有镜头
watch(impactFilterActive, (active) => {
  if (!active && impactFilter.value === 'affected') impactFilter.value = 'all'
})

/** 详情里提交生成任务后：刷新列表与任务，并启动进度轮询 */
function handleDetailSubmitted() {
  queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  queryClient.invalidateQueries({ queryKey: ['jobs', projectId, 'generate_clip'] })
  void trackClipProgress()
}

/** 以历史版本为基准重做：跳到分镜图工作台，由它按 base_asset_id 载入该版本配置 */
function handleRebase(payload: { shotId: string; assetId: string; version: number | null }) {
  detailVisible.value = false
  void router.push({
    name: 'storyboard-images',
    params: { id: projectId.value },
    query: {
      shot: payload.shotId,
      base: payload.assetId,
      ...(payload.version != null ? { basev: String(payload.version) } : {}),
    },
  })
}

// --- 出口：全部镜头视频生成完成后可去合成导出 ---
const allShots = computed(() => Object.values(shotsMap.value).flat())
/**
 * 全部镜头是否都已有视频（前端即时展示口径）。
 *
 * 注意：`!!s.current_assets?.clip` 只说明「列表里挂了个视频对象」，
 * 不说明文件真实存在、版本仍有效、生成任务成功结束。因此这里只用于
 * 按钮提示文案，**跳转判定一律走后端 stage-readiness**（会逐镜校验
 * current 版本 + 来源任务 succeeded + 文件存在，且覆盖全部集）。
 */
const allClipsReady = computed(() => {
  const shots = allShots.value
  return shots.length > 0 && shots.every((s) => !!s.current_assets?.clip)
})

const stageTransition = useStageTransition({
  projectId: () => projectId.value,
  stage: 'compose',
  to: (pid) => `/project/${pid}/compose`,
  notReadyTitle: '分镜视频尚未全部生成完成',
  confirmTitle: '确认进入合成与导出？',
  confirmDescription: [
    '所有分镜视频已经生成完成。',
    '请确认各镜头视频播放正常，人物动作、台词、音频、画面连续性均没有问题。',
    '确认后将进入合成与导出页面。',
  ].join('\n'),
  confirmText: '确认，进入合成与导出',
  notify: { warning: (m) => message.warning(m), error: (m) => message.error(m) },
  push: (path) => router.push(path),
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
  // 逐镜人工确认（keyframe_confirmed）已取消：只要该镜已有分镜图即可提交视频生成，
  // 更严格的「有效分镜图 + 文件存在」校验由后端 _check_keyframe_ready 统一负责。
  const ready = picked.filter((s) => !!s.current_assets?.keyframe)
  const pending = picked.length - ready.length
  if (!ready.length) {
    message.warning(`勾选的 ${picked.length} 个镜头都还没有分镜图，请先在分镜图环节生成`)
    return
  }
  if (pending > 0) {
    message.info(`本次提交 ${ready.length} 个已有分镜图的镜头，${pending} 个缺分镜图的镜头已跳过`)
  }
  // 弹窗选择批量生成方式：独立并行（不依赖上一镜尾帧）/ 链式串行（逐镜衔接）
  pendingClipShots.value = ready
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
    <!-- 左侧「制作流程」栏已高亮 04 分镜看板，页内不再重复同名大标题 -->
    <h1 class="visually-hidden">分镜看板</h1>
    <!-- 卡一：执行进度。只留集数选择 + 数字 + 进度条 + 在途/失败状态，
         状态句（正在生成视频 / 全部分镜视频已生成完成）与「最近完成于」已删除：
         数字与进度条已经回答「做到哪了」，「集数」二字与下方 chip 自身重复。
         在途明细并入本卡第二行，不再单独占一条横带。 -->
    <StageCard label="执行进度">
      <button
        v-for="ep in allEpisodes"
        :key="ep"
        class="episode-chip"
        :class="{ active: activeEpisode === ep }"
        @click="activeEpisode = ep"
      >第 {{ ep }} 集</button>
      <span v-if="allEpisodes.length" class="gen-sep" aria-hidden="true" />
      <!-- 进度数字与资产库/分镜图同构：已完成数加粗突出，分母与百分比次级色 -->
      <span class="stage-nums" :class="{ 'is-done': (clipProgress.data.value?.percent ?? 0) >= 100 }">
        <b>{{ clipsDoneCount }}</b><span class="sep">/</span>{{ allShots.length || clipProgress.data.value?.total || 0 }}
        <span class="pct">{{ clipProgress.data.value?.percent ?? 0 }}%</span>
      </span>
      <n-progress
        type="line"
        :percentage="clipProgress.data.value?.percent ?? 0"
        :height="6"
        :border-radius="3"
        :show-indicator="false"
        :color="clipProgress.data.value?.all_done ? 'var(--state-success)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle)"
        class="stage-progress"
      />

      <template #meta>
        <span v-if="clipProgress.data.value?.inflight_running" class="gen-state running">
          渲染中 {{ clipProgress.data.value.inflight_running }}
        </span>
        <span v-if="queuedClipCount" class="gen-state queued">
          排队/等待前序 {{ queuedClipCount }}
        </span>
        <span v-if="estimatedWaitText" class="gen-state">预计剩余 {{ estimatedWaitText }}</span>
        <span v-if="failedClipByShot.size" class="gen-state fail">
          最近失败 {{ failedClipByShot.size }} 个
        </span>
        <span v-if="clipProgress.error.value" class="gen-state fail">{{ clipProgress.error.value }}</span>
        <!-- 资产未齐全时的阻断说明：与进度同属状态陈述，放第二行 -->
        <span v-if="!readiness.all" class="gate-text">
          公共资产未齐全：{{ readiness.done }}/{{ readiness.total }}，生成分镜图前请先在资产库完成生成
        </span>
      </template>

      <template #actions>
        <n-button v-if="!readiness.all" size="small" quaternary @click="router.push(`/project/${projectId}/assets`)">
          去资产库
        </n-button>
        <n-button size="small" quaternary :loading="clipProgress.loading.value" @click="clipProgress.refresh()">
          刷新进度
        </n-button>
        <!-- 跳转按钮不禁用：未完成时点击会展示缺哪些镜头 -->
        <n-button
          size="small"
          type="primary"
          :loading="stageTransition.loading.value"
          :title="allClipsReady ? '进入合成与导出' : '点击查看还差哪些镜头'"
          @click="stageTransition.request()"
        >
          去合成导出
        </n-button>
      </template>
    </StageCard>

    <!-- 阶段跳转确认弹窗 -->
    <StageTransitionDialog
      v-model:visible="stageTransition.visible.value"
      :title="stageTransition.confirmTitle"
      :description="stageTransition.confirmDescription"
      :confirm-text="stageTransition.confirmText"
      :total="stageTransition.readiness.value.total"
      :done="stageTransition.readiness.value.done"
      :blockers="stageTransition.blockers.value"
      :ready="stageTransition.readiness.value.ready"
      :loading="stageTransition.loading.value"
      @confirm="stageTransition.confirm()"
      @cancel="stageTransition.cancel()"
    />

    <!-- 卡二：批量出视频（工作流 / 全选 / 批量出视频） -->
    <StageCard label="批量出视频">
      <span class="clip-wf-pick">
        <!-- 分镜看板只支持多参考图生视频：模型类型固定，不展示下拉，仅标签提示 -->
        <WorkflowSelector
          v-model="clipWorkflowId"
          slot="video"
          fixed-model-type="video_reference_to_video"
          size="small"
          placeholder="选择分镜视频工作流"
        />
      </span>
      <n-checkbox
        :checked="allShotsSelected"
        :disabled="!allShots.length"
        @update:checked="toggleSelectAllShots"
      >全选</n-checkbox>
      <span class="stage-sel">已选 <b>{{ selectedShotIds.length }}</b> / {{ allShots.length }}</span>

      <template #actions>
        <!-- 取消在途：任务中心已下线，这里是唯一能叫停链式批量的入口。
             只在真有在途任务时出现，避免常驻一个永远禁用的按钮。 -->
        <n-button
          v-if="clipInflightCount > 0"
          size="small"
          secondary
          type="warning"
          :loading="cancellingClip"
          :title="`取消排队 / 渲染中的 ${clipInflightCount} 个分镜视频任务`"
          @click="handleCancelClip"
        >
          取消在途（{{ clipInflightCount }}）
        </n-button>
        <n-button
          size="small"
          type="primary"
          :disabled="!readiness.all || !selectedShotIds.length"
          :loading="batchClipMutation.isPending.value"
          @click="handleBatchClip"
        >
          批量出视频（{{ selectedShotIds.length }}）
        </n-button>
      </template>
    </StageCard>

    <!-- 卡三：依赖影响（只读）。放在批量出视频之后：
         它回答「已经产出的东西是否因上游变化需要重做」，
         属于批量生成前的核对信息，而不是执行进度本身。 -->
    <StageCard label="依赖影响">
      <DependencyImpactOverview :project-id="projectId" @loaded="handleImpactLoaded" />
    </StageCard>

    <!-- 卡四：分镜规划导入（Phase 2）。校验只读；写入为显式安全 upsert，
         不覆盖锁定镜头、不删除旧镜头、不触发生成。 -->
    <StageCard label="分镜规划">
      <n-button secondary @click="planDialogVisible = true">导入规划 JSON</n-button>
      <template #meta>
        <span class="gen-state">校验只读；写入不覆盖锁定镜头、不删除旧镜头、不触发生成</span>
      </template>
    </StageCard>

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
      <!-- 依赖影响筛选：仅在有检查结果时出现，避免常驻无效控件 -->
      <div v-if="impactFilterActive" class="impact-filter-bar">
        <span class="impact-filter-label">依赖影响</span>
        <n-radio-group v-model:value="impactFilter" size="small">
          <n-radio value="all">全部镜头</n-radio>
          <n-radio value="affected">仅受影响（{{ affectedShotCount }}）</n-radio>
        </n-radio-group>
        <span class="impact-filter-hint">来自卡三最近一次检查结果，仅影响显示</span>
      </div>

      <div v-for="scene in displayScenes" :key="scene.id" class="scene-group">
        <div class="scene-header">
          <span class="scene-seq">#{{ scene.seq }}</span>
          <span class="scene-title">{{ scene.title }}</span>
          <n-tag v-if="scene.episodes?.length" size="tiny" round>
            第 {{ scene.episodes.join(', ') }} 集
          </n-tag>
          <span v-if="impactFilter === 'affected'" class="shot-count">
            {{ visibleShots(scene.id).length }} / {{ scene.shot_count ?? 0 }} 镜头
          </span>
          <span v-else class="shot-count">{{ scene.shot_count ?? 0 }} 镜头</span>
        </div>

        <div
          class="shots-scroll"
          :class="{ 'is-overflow': rowOverflow[scene.id] }"
        >
          <div
            ref="rowRefs"
            class="shots-row"
            :data-scene="scene.id"
            :style="{ '--shot-thumb-ratio': shotThumbRatio, '--shot-card-w': shotCardWidth }"
            @scroll="onRowScroll($event, scene.id)"
            @dragover="onDragOver($event, scene.id)"
          >
          <n-spin v-if="loadingShots[scene.id]" size="small" />
          <template v-else>
            <div
              v-for="shot in visibleShots(scene.id)"
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
                  :label="`选择镜头 ${shotGlobalSeq(shot)}`"
                  :checked="selectedShotIds.includes(shot.id)"
                  @update:checked="(v: boolean) => toggleShot(shot.id, v)"
                  @click.stop
                />
                <div class="shot-thumb">
                  <img
                    v-if="shot.current_assets?.keyframe?.thumb_url || shot.current_assets?.keyframe?.url"
                    :src="shot.current_assets.keyframe!.thumb_url || shot.current_assets.keyframe!.url"
                    :alt="`镜头 ${shot.seq ?? shot.id} 分镜图`"
                    loading="lazy"
                  />
                  <div v-else class="thumb-placeholder">
                    <span>{{ shotKindLabels[shot.shot_kind] }}</span>
                  </div>
                  <!-- 依赖影响角标（只读提示）：来自卡三「检查依赖影响」结果，点击卡片打开详情可查看影响面板 -->
                  <div
                    v-if="shotImpactByShotId.has(shot.id)"
                    class="impact-badge"
                    :class="`is-${shotImpactByShotId.get(shot.id)}`"
                    :title="shotImpactLabel(shot.id)"
                  >!</div>
                  <!-- 工作流版本影响角标（只读提示）：执行时版本落后或工作流已停用 -->
                  <div
                    v-if="workflowImpactByShot.has(shot.id)"
                    class="workflow-badge"
                    :class="{ 'is-disabled': workflowImpactByShot.get(shot.id)?.change_status === 'workflow_disabled' }"
                    :title="workflowImpactLabel(shot.id)"
                  >{{ workflowImpactBadgeText(shot.id) }}</div>
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
          <!-- 横向溢出提示：右侧渐隐在近黑底上几乎不可见（淡出成透明 = 还是黑的），
               改用显式计数 + 箭头，用户一眼知道「还有 N 镜在右边」。 -->
          <button
            v-if="rowOverflow[scene.id]"
            type="button"
            class="row-more"
            :aria-label="`${scene.title} 还有更多镜头，向右滚动`"
            @click="scrollRowRight(scene.id)"
          >
            <span class="row-more-text">还有更多</span>
            <span class="row-more-arrow" aria-hidden="true">→</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 镜头详情：左预览 + 右三标签页（提示词 / 参考图 / 生成），支持 ←/→/Enter/Esc/R -->
    <ShotDetailModal
      v-model:show="detailVisible"
      v-model:shot-id="detailShotId"
      :shots="orderedShots"
      :project-id="projectId"
      :readiness-all="readiness.all"
      :status-kind="detailStatus?.kind ?? 'stale'"
      :status-label="detailStatus?.label ?? '未知状态'"
      :kind-label="activeShot ? shotKindLabels[activeShot.shot_kind] : ''"
      :clip-job-active="detailClipJobActive"
      :clip-workflow-id="clipWorkflowId"
      :scenes="scenesQuery.data.value?.items ?? []"
      :characters="charsData?.items ?? []"
      :prop-items="propsData?.items ?? []"
      @submitted="handleDetailSubmitted"
      @prompts-saved="handlePromptsSaved"
      @rebase="handleRebase"
    />
    <!-- 分镜规划导入：粘贴 JSON → 校验预览 → 显式写入 -->
    <StoryboardPlanImportDialog
      v-model:show="planDialogVisible"
      :project-id="projectId"
      @applied="refreshStoryboard"
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
          <span style="font-size: 13px; color: var(--text-secondary)">视频工作流</span>
          <WorkflowSelector
            v-model="clipWorkflowId"
            slot="video"
            fixed-model-type="video_reference_to_video"
            size="small"
            style="width: 240px"
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
.episode-chip { border: 1px solid var(--border-subtle); background: var(--bg-panel); border-radius: var(--radius); padding: 6px var(--sp-4); font-size: 14px; color: var(--text-secondary); cursor: pointer; transition: border-color var(--transition-fast), color var(--transition-fast); }
.episode-chip:hover { border-color: var(--brand-primary); color: var(--brand-primary); }
.episode-chip.active { background: var(--brand-soft); border-color: var(--brand-primary); color: var(--brand-primary); font-weight: 600; }

/* ===== 执行进度卡内容（容器样式由 StageCard 提供） ===== */
.gen-sep {
  width: 1px;
  height: 16px;
  background: var(--border-subtle);
  flex: none;
}
/* 进度数字（.stage-nums）与进度条（.stage-progress）的样式已收进 global.css，
   三页「执行进度」卡共用同一份口径。这里曾写 `.gen-bar { flex: 1 1 160px }`
   与 `.gen-nums` 一整套，与资产库的 4px 条 / 分镜图的 `flex: 1` 都不一致。 */
.gen-state { color: var(--text-secondary); }
.gen-state.queued { color: var(--state-warning); }
.gen-state.running { color: var(--brand-primary); }
.gen-state.fail { color: var(--state-danger); }
.gate-text { color: var(--danger-text, var(--state-danger)); }

/* 「已选 X / Y」用全局 .stage-sel（三页共用一份口径）。
   此前本页没有这条规则，`.sel-count` 落到 body 的 14px，比另两页大 2px。 */
/* 工作流选择器槽位：给最小宽度，避免 loading spinner 或错误态把槽位塌缩成 12px。
   实测后端 /workflows/candidates 失败时，选择器渲染成 n-spin，无 min-width 时
   整个槽位只有 12px，卡二看起来像「标签与内容错位」。 */
.clip-wf-pick {
  display: block;
  flex: 0 1 210px;
  min-width: 170px;
  max-width: 260px;
}

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
.impact-filter-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-panel);
}
.impact-filter-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.impact-filter-hint {
  font-size: 11px;
  color: var(--text-tertiary);
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
/* 横向滚动容器：外层负责定位提示按钮，内层负责滚动 */
.shots-scroll {
  position: relative;
}
.shots-row {
  display: flex;
  gap: var(--sp-3);
  overflow-x: auto;
  padding-bottom: var(--sp-2);
  scroll-snap-type: x proximity;
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
}
.shots-row::-webkit-scrollbar {
  height: 6px;
}
.shots-row::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border: 0;
  border-radius: var(--radius-pill);
}
.shots-row::-webkit-scrollbar-track {
  background: transparent;
}
/* 溢出提示：右侧浮一枚「还有更多 →」。
   曾用 mask-image 渐隐，但近黑背景下淡出成透明与背景同色，
   视觉上完全看不出（1920/1366 截图均证实），因此改为显式可点击提示。 */
.row-more {
  position: absolute;
  top: 50%;
  right: 0;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  font-family: inherit;
  font-size: 12px;
  line-height: 1;
  color: var(--brand-primary);
  background: var(--bg-elevated);
  border: 1px solid var(--brand-line);
  border-radius: var(--radius-pill);
  box-shadow: var(--shadow-lg);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    border-color var(--transition-fast),
    background-color var(--transition-fast);
}
.row-more:hover {
  color: var(--text-on-brand);
  background: var(--brand-gradient);
  border-color: transparent;
}
.row-more:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
.row-more-arrow {
  font-size: 13px;
  line-height: 1;
}
.shot-card-wrapper {
  position: relative;
  flex-shrink: 0;
  scroll-snap-align: start;
}
/* 卡片高度由内容决定（此前写死 320px）：缩略图 200px + 信息区约 54px ≈ 254px，
   底部固定留出 66px 空白，26 张卡叠起来就是一片「空卡片」观感。 */
/* 卡片宽度随画幅自适应（--shot-card-w）：
   9:16 竖屏若沿用 220px 宽，缩略图会被撑到 391px 高、整卡 450px，
   一屏只能看到 5 张多，26 镜的场景要横滑很久。竖屏收窄到 168px，
   缩略图约 299px、整卡约 360px，同屏可见 7 张且构图不被裁。
   横屏反之需要更宽，方形居中。 */
.shot-card {
  width: var(--shot-card-w, 200px);
  height: auto;
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
/* label 只为提供可访问名（aria-labelledby 引用它），视觉上不显示 */
.shot-check :deep(.n-checkbox__label) {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
  border: 0;
}
/* 缩略图比例跟随项目画幅（--shot-thumb-ratio），不再固定 200px：
   9:16 项目在近方形窗口里会被 object-fit:cover 裁掉上下构图。
   min-height:0 必须显式声明，否则 flex item 的 min-height:auto 会撑破比例。 */
.shot-thumb {
  position: relative;
  width: 100%;
  aspect-ratio: var(--shot-thumb-ratio, 4 / 5);
  min-height: 0;
  background: var(--bg-elevated);
  overflow: hidden;
}
.shot-thumb img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
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
/* 依赖影响角标：缩略图左上角常驻，区别于右下角的生产状态徽章。
   位置避开左上角的选择框（.shot-check 挂在卡片层，z-index 更低不会重叠）。 */
.impact-badge {
  position: absolute;
  top: 6px;
  right: 6px;
  z-index: 2;
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  color: var(--text-on-brand);
  background: var(--state-warning);
  cursor: help;
}
.impact-badge.is-redo_required {
  background: var(--state-danger);
}
/* 工作流版本影响角标：缩略图左下角，文字型提示，避免与左上依赖角标和右下状态徽章重叠 */
.workflow-badge {
  position: absolute;
  left: 6px;
  bottom: 6px;
  z-index: 2;
  padding: 2px 6px;
  border: 1px solid var(--state-warning);
  border-radius: var(--radius);
  background: var(--bg-elevated);
  color: var(--state-warning);
  font-size: 11px;
  line-height: 1.2;
  white-space: nowrap;
  cursor: help;
}
.workflow-badge.is-disabled {
  border-color: var(--danger-text);
  color: var(--danger-text);
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
