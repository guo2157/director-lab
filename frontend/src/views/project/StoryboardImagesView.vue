<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NIcon, NProgress, useMessage } from 'naive-ui'
import { ArrowForwardOutline } from '@vicons/ionicons5'
import { getShot, listScenes, listSceneShots } from '@/api/script'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { cancelProjectJobs } from '@/api/jobs'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { replaceAsset } from '@/api/assets'
import { getProject } from '@/api/projects'
import { getKeyframeGenerationProgress } from '@/api/generation'
import { buildShotImpactMap, getProjectImpactPreview } from '@/api/dependencies'
import type { DependencyChangeStatus } from '@/api/dependencies'
import { relativeTime } from '@/utils/time'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { Shot } from '@/types/script'
import type { KeyframeGenerationJob, KeyframeGenerationProgress } from '@/types/generation'
import type { GenerateShotPayload } from '@/types/job'
import StageCard from '@/components/ui/StageCard.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import ShotCard from '@/components/storyboard-images/ShotCard.vue'
import BatchToolbar from '@/components/storyboard-images/BatchToolbar.vue'
import ShotDetailDialog from '@/components/storyboard-images/ShotDetailDialog.vue'
import StageTransitionDialog from '@/components/project/StageTransitionDialog.vue'
import { useStageTransition } from '@/composables/useStageTransition'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const projectId = computed(() => route.params.id as string)

// 项目数据：只用于取画幅比例（分镜图卡片按项目比例展示）
// 资产审核状态已取消：分镜图能否生成由后端 assets_gate 的产物校验决定，
// 前端不再读 assets_review_status，也不再因未审核禁用生成按钮。
const projectQuery = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
})

// 画幅：分镜图卡片按项目比例展示，竖屏项目不再被 16:9 裁切
const aspectPair = computed(() => {
  const raw = projectQuery.data.value?.aspect_ratio || '16:9'
  const [w, h] = raw.split(':').map((n) => Number(n) || 0)
  return w > 0 && h > 0 ? { w, h } : { w: 16, h: 9 }
})
const isPortrait = computed(() => aspectPair.value.h > aspectPair.value.w)
const thumbRatio = computed(() => `${aspectPair.value.w} / ${aspectPair.value.h}`)

const scenesQuery = useQuery({
  queryKey: ['scenes', projectId],
  queryFn: () => listScenes(projectId.value, { include_shots: false }),
})

/**
 * 镜头缓存键：`${sceneId}::${episode}`。
 *
 * 为什么不能只用 sceneId（实测事故 prj_9db2e016b337「海风与夏日」）：
 * 场景是项目级、跨集复用的（同一地点多集只建一条 Scene 记录，集数关系存
 * scene_episode 关联表）。旧实现键只有 sceneId，且加载时写死
 * `sc.episodes?.[0] ?? 1` —— 多集项目里第 2 集及以后的镜头永远不会被请求，
 * 页面「分镜图」只显示第 1 集，而解析页明明写着「第 2 集 11 个分镜」。
 * 键里带上集号，同一场景的多集镜头才能共存。
 */
const shotsMap = ref<Record<string, Shot[]>>({})
const loadingShots = ref<Record<string, boolean>>({})
const promptDraft = ref<Record<string, string>>({})
const shotKey = (sceneId: string, episode: number) => `${sceneId}::${episode}`

/** 全量拉取场景内镜头：进度统计必须覆盖全场镜头，不能只看第一页（单页上限 100） */
async function loadSceneShots(sceneId: string, episode: number) {
  const key = shotKey(sceneId, episode)
  loadingShots.value[key] = true
  try {
    const all: Shot[] = []
    let page = 1
    for (;;) {
      const res = await listSceneShots(sceneId, { episode, page, page_size: 100 })
      const batch = res?.items ?? []
      all.push(...batch)
      if (batch.length < 100 || all.length >= (res?.total ?? 0)) break
      page += 1
    }
    shotsMap.value[key] = all
  } catch (e: any) {
    message.error(`加载镜头失败: ${e?.message ?? e}`)
  } finally {
    loadingShots.value[key] = false
  }
}

// --- 集选择器（与分镜看板同一交互：集数 chip 切换） ---
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

/** 当前集的场景：只保留在该集出现过的场景 */
const episodeScenes = computed(() => {
  const items = scenesQuery.data.value?.items ?? []
  const ep = activeEpisode.value
  if (ep === null) return items
  return items.filter((s) => (s.episodes ?? []).includes(ep))
})

watch(
  [() => scenesQuery.data.value?.items ?? [], activeEpisode],
  ([scenes, ep]) => {
    if (ep === null) return
    for (const sc of scenes) {
      // 只加载「该场景确实在这一集出现过」的组合，避免请求不存在的关系
      if (!(sc.episodes ?? []).includes(ep)) continue
      const key = shotKey(sc.id, ep)
      if (!shotsMap.value[key]) void loadSceneShots(sc.id, ep)
    }
  },
  { immediate: true },
)

/** 当前集全部镜头（页面所有统计与批量的口径都限定在本集） */
const allShots = computed(() => {
  const ep = activeEpisode.value
  if (ep === null) return Object.values(shotsMap.value).flat()
  const out: Shot[] = []
  for (const sc of episodeScenes.value) {
    out.push(...(shotsMap.value[shotKey(sc.id, ep)] ?? []))
  }
  return out
})
// --- 集内连续镜头序号 ---
// shot.seq 是场景内序号，各场景都会从 1 开始；按场景 seq 升序 + 场景内镜头 seq 升序累加得到 1..N 的集内镜号。
const orderedShots = computed<Shot[]>(() => {
  const ep = activeEpisode.value
  const scenes = [...episodeScenes.value].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
  const list: Shot[] = []
  for (const sc of scenes) {
    const shots = ep === null
      ? []
      : [...(shotsMap.value[shotKey(sc.id, ep)] ?? [])].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
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
const keyframeShots = computed(() => allShots.value.filter((s) => !!s.current_assets?.keyframe))
/**
 * 全部镜头是否都已有当前有效分镜图（进入分镜看板的前端展示口径）。
 *
 * 注意：这里只用于进度条/按钮提示的即时反馈，**不是跳转依据**——
 * 跳转前一律以后端 stage-readiness 的 ready 为准（会额外校验文件真实存在、
 * 版本有效、失败任务等前端拿不到的信息）。
 *
 * 变量名从 allConfirmed 改为 allStoryboardImagesReady：逐镜人工确认已取消，
 * 沿用「confirmed」会让人误以为还存在确认标记这层语义。
 */
const allStoryboardImagesReady = computed(
  () => allShots.value.length > 0 && allShots.value.every((s) => !!s.current_assets?.keyframe),
)

// --- 生成进度（后端聚合：整体 X/Y、正在生成哪几镜、是否全部完成） ---
const keyframeProgress = useGenerationProgress<KeyframeGenerationProgress>({
  fetcher: () => getKeyframeGenerationProgress(projectId.value),
  onChange: () => {
    // 出图完成后立即刷新镜头，让分镜图缩略图与状态同步
    refreshAll()
  },
  onAllComplete: () => {
    message.success('全部分镜图已生成完成，可进入分镜看板')
  },
})

/** 在途任务 → 镜头 ID 状态（卡片直接显示排队/生成中 42%，无需逐镜点开） */
const inflightByShot = computed(() => {
  const map = new Map<string, KeyframeGenerationJob>()
  for (const job of keyframeProgress.data.value?.inflight ?? []) {
    if (job.shot_id) map.set(job.shot_id, job)
  }
  return map
})

/** 最近失败镜头（按镜头去重，取最近一次失败原因） */
const failedByShot = computed(() => {
  const map = new Map<string, string>()
  for (const f of keyframeProgress.data.value?.failed_recent ?? []) {
    if (f.shot_id && !map.has(f.shot_id)) map.set(f.shot_id, f.error_msg || '未知原因')
  }
  return map
})

/** 顶部进度条数值：已出图 / 全部镜头 */
const progressPercent = computed(() => {
  const total = allShots.value.length
  if (!total) return 0
  return Math.round((keyframeShots.value.length / total) * 100)
})

/** 顶部进度数字已改为模板内直接绑定（与资产库同构：已完成数加粗 + 分母/百分比次级色），
 *  不再需要拼接字符串的 computed。 */

function inflightTextOf(shot: Shot): string {
  const job = inflightByShot.value.get(shot.id)
  if (!job) return ''
  if (job.status === 'running') return job.progress != null ? `生成中 ${job.progress}%` : '生成中'
  if (job.status === 'queued') return '排队中'
  return '提交确认中'
}

/** 提交生成任务后：立即建立轮询基线，全部结束后统一提示 */
async function trackAfterSubmit() {
  await keyframeProgress.start({ notifyOnComplete: true })
}

/**
 * 批量取消在途出图任务（2026-11-21）：
 * 任务中心页面已下线，逐条取消的 UI 不复存在；批量出图跑错工作流 / 选错镜头时
 * 用户没有任何「停下来」的入口，只能等整批跑完白烧显卡。这里提供按 stage 批量取消。
 */
const cancellingInflight = ref(false)
const confirmDialog = useConfirmDialog()
function handleCancelInflight() {
  const n = keyframeProgress.data.value?.inflight_count ?? 0
  if (!n) return
  confirmDialog({
    title: '取消进行中的出图任务',
    message: `将取消当前批次 ${n} 个还在排队 / 运行中的分镜图任务（正在渲染的会同时中断 ComfyUI）。已生成完成的图片不受影响。`,
    impact: [`在途出图任务 ${n} 个`],
    confirmText: '取消这些任务',
    cancelText: '继续生成',
    onConfirm: async () => {
      cancellingInflight.value = true
      try {
        const res = await cancelProjectJobs(projectId.value, { target: 'keyframe' })
        message.success(
          `已取消 ${res.jobs} 个出图任务${res.interrupted ? `（中断渲染 ${res.interrupted} 个）` : ''}`,
        )
        refreshAll()
        await keyframeProgress.refresh()
      } catch (e: any) {
        message.error(`取消失败: ${e?.message ?? e}`)
      } finally {
        cancellingInflight.value = false
      }
    },
  })
}

/** 失败镜头重试：仅重提当前失败的镜头 */
const retryingFailed = ref(false)
async function retryFailedShots() {
  const ids = [...failedByShot.value.keys()]
  if (!ids.length) return
  retryingFailed.value = true
  try {
    await generateProjectShots(projectId.value, {
    shot_ids: ids,
    target: 'keyframe',
    workflow_id: generateWorkflowId.value,
  })
    message.success(`已重新提交 ${ids.length} 张失败分镜图`)
    refreshAll()
    await trackAfterSubmit()
  } catch (e: any) {
    message.error(`重试失败: ${e?.message ?? e}`)
  } finally {
    retryingFailed.value = false
  }
}

// 页面进入/刷新：有在途任务才轮询，保证刷新后仍能看到总体进度
void keyframeProgress.bootstrap()

/** 正在提交的镜头：主按钮即时反馈，避免重复点击 */
const pendingShotId = ref('')

/**
 * 本页出图工作流（storyboard_image 槽位）。
 * 候选由后端 /workflows/candidates 按真实能力过滤：必须能接多张参考图
 * （角色 / 场景 / 道具），纯文生图不会进入候选。
 * 选择器位于批量生成按钮左侧，未选择时自动落到默认工作流；
 * 单张、批量、重新生成、失败重试共用这一份选择，不再有隐式按能力匹配。
 */
const generateWorkflowId = ref<string | undefined>(undefined)

const generateMutation = useMutation({
  mutationFn: (payload: { shotId: string; params?: Record<string, string> }) =>
    generateShot(payload.shotId, {
      target: 'keyframe',
      params: payload.params,
      workflow_id: generateWorkflowId.value,
    } as GenerateShotPayload),
  onSuccess: async () => {
    message.success('分镜图生成任务已提交，正在生成（进度见顶部进度条）')
    // 提交后立即建立在途基线并轮询，全部结束后提示「全部分镜图已生成完成」
    await trackAfterSubmit()
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
  onSettled: () => {
    pendingShotId.value = ''
  },
})

function handleGenerate(shot: Shot) {
  const params: Record<string, string> = {}
  if (promptDraft.value[shot.id]) params.prompt = promptDraft.value[shot.id]
  pendingShotId.value = shot.id
  generateMutation.mutate({ shotId: shot.id, params })
}

// 勾选与批量出图
const selectedIds = ref<Record<string, boolean>>({})
const selectedList = computed(() => allShots.value.filter((s) => selectedIds.value[s.id]))
const allSelected = computed(() => allShots.value.length > 0 && selectedList.value.length === allShots.value.length)
const busyShot = ref<string>('')

function toggleSelect(shot: Shot, checked: boolean) {
  selectedIds.value[shot.id] = checked
}

function toggleSelectAll(checked: boolean) {
  if (!checked) {
    selectedIds.value = {}
    return
  }
  const next: Record<string, boolean> = {}
  for (const s of allShots.value) next[s.id] = true
  selectedIds.value = next
}

function refreshAll() {
  const ep = activeEpisode.value
  if (ep === null) return Promise.resolve([])
  const jobs = episodeScenes.value.map((sc) => loadSceneShots(sc.id, ep))
  return Promise.all(jobs)
}

// --- 批量生成前的依赖影响预检（Phase 3 / 10，只读） ---
// 用户点「依赖预检」或提交批量生成时，拉一次项目级预演，把勾选镜头中
// 受上游资产/依赖变化影响的列出来，由用户决定是否继续；不自动重做、不写库。
// 预检失败不阻断提交（接口只读、可能因服务异常失败），只提示本次未做预检。
const impactByShotId = ref<Map<string, DependencyChangeStatus>>(new Map())
const impactChecked = ref(false)
const impactCheckedAt = ref('')
const impactChecking = ref(false)
const impactError = ref('')

const selectedImpactShots = computed(() =>
  impactChecked.value ? selectedList.value.filter((s) => impactByShotId.value.has(s.id)) : [],
)
const impactNote = computed(() => {
  if (!impactChecked.value || !selectedList.value.length) return ''
  const n = selectedImpactShots.value.length
  return n
    ? `所选 ${n} / ${selectedList.value.length} 个镜头受上游变化影响，生成前请确认`
    : '所选镜头无上游依赖变化'
})

async function ensureImpactChecked(): Promise<boolean> {
  if (impactChecked.value) return true
  impactChecking.value = true
  impactError.value = ''
  try {
    const data = await getProjectImpactPreview(projectId.value)
    impactByShotId.value = buildShotImpactMap(data)
    impactCheckedAt.value = new Date().toISOString()
    impactChecked.value = true
    return true
  } catch (e: any) {
    impactError.value = e?.message ?? '依赖预检失败'
    return false
  } finally {
    impactChecking.value = false
  }
}

/** 手动重新预检：勾选变化后可用它刷新结果（不自动请求） */
async function runImpactCheck() {
  impactChecked.value = false
  if (!(await ensureImpactChecked())) {
    message.warning(`依赖预检失败：${impactError.value}`)
  }
}

const batchMutation = useMutation({
  mutationFn: (ids: string[]) =>
    generateProjectShots(projectId.value, {
      shot_ids: ids,
      target: 'keyframe',
      workflow_id: generateWorkflowId.value,
    }),
  onSuccess: async (res: any) => {
    message.success(`已提交 ${res?.count ?? 0} 个分镜图任务，正在生成（进度见顶部进度条）`)
    await trackAfterSubmit()
  },
  onError: (e: any) => message.error(`批量生成失败: ${e?.message ?? e}`),
})

async function handleBatchGenerate() {
  const picked = selectedList.value
  if (!picked.length) {
    message.warning('请先勾选需要生成分镜图的镜头')
    return
  }
  // 不再有前端审核前置与锁定检查：生成条件由后端 assets_gate 权威校验
  // （必需资产是否真实生成完成），前端重复判断只会造成两套口径。
  const checked = await ensureImpactChecked()
  if (!checked) {
    message.warning(`依赖预检失败（${impactError.value}），本次批量生成未做依赖影响检查`)
    void submitByPlans(picked)
    return
  }
  const affected = picked.filter((s) => impactByShotId.value.has(s.id))
  if (!affected.length) {
    void submitByPlans(picked)
    return
  }
  // 受上游变化影响的镜头逐条列出（最多 6 条，其余合并计数），
  // 让用户带着影响范围决定是否继续，而不是静默按旧依赖重画。
  const impact = affected.slice(0, 6).map((s) => `镜头 #${shotGlobalSeq(s)}`)
  if (affected.length > impact.length) impact.push(`另有 ${affected.length - impact.length} 个镜头`)
  confirmDialog({
    title: '所选镜头存在依赖变化',
    message: '上游资产或依赖已变化：继续批量生成会用当前资产重新出图。',
    impact,
    confirmText: '仍然批量生成',
    cancelText: '取消',
    danger: false,
    onConfirm: () => submitByPlans(picked),
  })
}

/** 按已定稿定义批量提交：名称与提示词以库中定义为准；定义缺失时回退镜头级批量 */
async function submitByPlans(picked: Shot[]) {
  try {
    const res = await listAssetPlans(projectId.value, { plan_type: 'keyframe' })
    const pickedIds = new Set(picked.map((s) => s.id))
    // 在途镜头不重复提交：任务还在排队/生成中时库里没有产物，
    // 后端判重也查不到（现已修，但前端仍应提前拦住，减少无效往返）。
    const inflightIds = new Set(inflightByShot.value.keys())
    const planIds = (res.items || [])
      .filter((p) => p.owner_id && pickedIds.has(p.owner_id) && !inflightIds.has(p.owner_id))
      .map((p) => p.id)
    const skippedInflight = picked.filter((s) => inflightIds.has(s.id)).length
    if (!planIds.length) {
      if (skippedInflight) {
        message.info(`${skippedInflight} 个分镜图正在生成中，请等待完成后再操作`)
      } else {
        batchMutation.mutate(picked.map((s) => s.id))
      }
      return
    }
    const gen = await generateFromPlans(projectId.value, { plan_type: 'keyframe', plan_ids: planIds })
    // already_generated 单独提示：用户点了「全选」时最需要知道的就是
    // 「哪些没被重做、为什么」——原始 reason 代码（already_generated）不可读
    const doneCount = (gen.skipped ?? []).filter((s: any) => s.reason === 'already_generated').length
    const otherSkipped = (gen.skipped ?? []).filter((s: any) => s.reason !== 'already_generated')
    if (gen.count > 0) {
      message.success(
        doneCount
          ? `已提交 ${gen.count} 个分镜图任务，跳过 ${doneCount} 个已生成的`
          : `已提交 ${gen.count} 个分镜图任务，正在生成（进度见顶部进度条）`,
      )
    } else if (doneCount) {
      message.info(`${doneCount} 个分镜图已生成过，无需重做；如需重画请在镜头卡片上点「重新生成」`)
    }
    if (otherSkipped.length) {
      const reasons = [...new Set(otherSkipped.map((s: any) => s.reason))].join('、')
      message.info(`另有 ${otherSkipped.length} 条定义被跳过：${reasons}`)
    }
    await trackAfterSubmit()
  } catch (e: any) {
    // 幂等：定义对应的分镜图均已生成过 / 正在生成，属于预期跳过而非失败。
    // 后端 no_generatable_plans 会带回 skipped_reasons，据此给出可操作的提示，
    // 而不是把「都生成过了」当成错误弹红条。
    if (e?.code === 'no_generatable_plans' || e?.code === 'already_generated') {
      const reasons: Record<string, number> =
        (e?.detail as Record<string, any>)?.skipped_reasons ?? {}
      const inflight = reasons['already_inflight'] ?? 0
      const done = reasons['already_generated'] ?? 0
      if (inflight && !done) {
        message.info(`${inflight} 个分镜图正在生成中，请等待完成后再操作`)
      } else if (done && !inflight) {
        message.info(`${done} 个分镜图已生成过，无需重做；如需重画请在镜头卡片上点「重新生成」`)
      } else {
        message.info('所选分镜图均已生成过或正在生成中，已全部跳过')
      }
      await trackAfterSubmit()
      return
    }
    message.error(`批量生成失败: ${e?.message ?? e}`)
  }
}

// 替换上传：人工替换当前分镜图，保持与镜头的关联
const replaceTarget = ref<Shot | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)

function pickReplace(shot: Shot) {
  const kf = shot.current_assets?.keyframe
  if (!kf?.asset_id) {
    message.warning('该镜暂无分镜图，请先生成后再替换')
    return
  }
  replaceTarget.value = shot
  fileInputRef.value?.click()
}

async function onReplaceFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  const target = replaceTarget.value
  replaceTarget.value = null
  input.value = ''
  if (!file || !target) return
  const kf = target.current_assets?.keyframe
  if (!kf?.asset_id) return
  busyShot.value = target.id
  try {
    await replaceAsset(kf.asset_id, file)
    message.success('已替换该镜分镜图')
    await refreshAll()
    // 详情弹窗开着时同步刷新，避免停留在替换前的旧图
    if (dialogRef.value?.visible) {
      const updated = allShots.value.find((s) => s.id === target.id)
      if (updated) dialogRef.value?.open(updated)
    }
  } catch (err: any) {
    message.error(`替换失败: ${err?.message ?? err}`)
  } finally {
    busyShot.value = ''
  }
}

// --- 出口：去分镜看板（查后端就绪 → 确认弹窗 → 确认时再查一次 → 跳转）---
const stageTransition = useStageTransition({
  projectId: () => projectId.value,
  stage: 'storyboard_images',
  to: (pid) => `/project/${pid}/storyboard`,
  notReadyTitle: '分镜图片尚未全部生成完成',
  confirmTitle: '确认进入分镜看板？',
  confirmDescription: [
    '所有分镜图片已经生成完成。',
    '请确认分镜图片的构图、人物形象、角色站位、场景和道具均没有问题。',
    '确认后将进入分镜看板，开始生成分镜视频。',
  ].join('\n'),
  confirmText: '确认，进入分镜看板',
  notify: { warning: (m) => message.warning(m), error: (m) => message.error(m) },
  push: (path) => router.push(path),
})

// --- 网格分组 ---
/** 场景分组：只列当前集的场景，组内按集内镜号顺序。状态筛选 chips 已删除，
 *  所有镜头始终全量展示——进度卡的数字与卡片自身的状态标签已足够定位。 */
const sceneGroups = computed(() => {
  const ep = activeEpisode.value
  return [...episodeScenes.value]
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
    .map((scene) => ({
      scene,
      episode: ep ?? 1,
      shots: ep === null
        ? []
        : [...(shotsMap.value[shotKey(scene.id, ep)] ?? [])].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)),
    }))
})

/** 场景列表（详情弹窗需要场景名与基准图用于推导参考图） */
const scenesLite = computed(
  () =>
    (scenesQuery.data.value?.items ?? []) as Array<{ id: string; title: string; baseline_asset_id?: string | null }>,
)

const loading = computed(
  () => scenesQuery.isLoading.value || (allShots.value.length === 0 && Object.values(loadingShots.value).some(Boolean)),
)

// --- 详情弹窗 ---
const dialogRef = ref<InstanceType<typeof ShotDetailDialog> | null>(null)

function openDetail(shot: Shot) {
  dialogRef.value?.open(shot)
}

/**
 * 从分镜看板「以历史版本为基准重做」跳转而来：`?shot=<id>&base=<asset_id>&basev=<n>`。
 *
 * 先按 shot_id 拉取镜头元信息确定所属集数（可能不是当前集），切集后等该集镜头
 * 加载完成再打开详情，并把 base 版本透传给工作台。打开后清掉 query，
 * 避免刷新或返回时重复弹出。
 */
const pendingOpen = ref<{
  shotId: string
  base: { id: string; version: number | null } | null
} | null>(null)

watch(
  [() => route.query.shot, () => route.query.base, () => route.query.basev],
  async ([shotId, baseId, basev]) => {
    if (typeof shotId !== 'string' || !shotId) return
    try {
      const shot = await getShot(shotId)
      if (shot.episode !== activeEpisode.value) activeEpisode.value = shot.episode
      pendingOpen.value = {
        shotId,
        base:
          typeof baseId === 'string' && baseId
            ? { id: baseId, version: basev ? Number(basev) || null : null }
            : null,
      }
    } catch (e: any) {
      message.error(`打开镜头失败：${e?.message ?? e}`)
      clearShotQuery()
    }
  },
  { immediate: true },
)

watch(
  [allShots, pendingOpen],
  async ([shots, pending]) => {
    if (!pending) return
    const shot = shots.find((s) => s.id === pending.shotId)
    if (!shot) return
    pendingOpen.value = null
    await nextTick()
    dialogRef.value?.open(shot, pending.base ?? undefined)
    clearShotQuery()
  },
  { immediate: true },
)

/** 清掉跳转参数，保留其余 query */
function clearShotQuery() {
  const q = { ...route.query }
  delete q.shot
  delete q.base
  delete q.basev
  void router.replace({ query: q })
}

/** 详情里首次生成：同步工作台提示词后走镜头级生成链路 */
function onFirstGenerate(shot: Shot, prompt: string) {
  promptDraft.value[shot.id] = prompt
  handleGenerate(shot)
}
</script>

<template>
  <div class="page sb-images-page">
    <!-- 左侧「制作流程」栏已高亮 03 分镜图，页内不再重复同名大标题。
         审核状态与「去分镜看板」已并入下方进度卡的右端，页头不再单独占一行。 -->
    <h1 class="visually-hidden">分镜图</h1>

    <input ref="fileInputRef" type="file" accept="image/*" class="hidden-file" @change="onReplaceFileChange" />

    <!-- 卡一：执行进度。只留数字 + 进度条 + 在途/失败状态，
         状态句（正在生成 N 张 / 已全部生成完成）与审核通过标签已删除：
         数字与进度条已经回答「做到哪了」，通过后的审核标签是已完成的事。
         正在生成的镜头明细并入本卡第二行，不再单独占一条横带。 -->
    <StageCard label="执行进度">
      <!-- 集数选择：多集项目必须能按集切换。旧实现没有这个入口，且加载逻辑写死
           `episodes[0]`，导致「分镜图」页永远只显示第 1 集（实测 prj_9db2e016b337
           第 2 集 11 个分镜在解析页可见、这里完全看不到）。交互与分镜看板一致。 -->
      <button
        v-for="ep in allEpisodes"
        :key="ep"
        class="episode-chip"
        :class="{ active: activeEpisode === ep }"
        @click="activeEpisode = ep"
      >第 {{ ep }} 集</button>
      <span v-if="allEpisodes.length" class="gen-sep" aria-hidden="true" />
      <!-- 进度数字与资产库同构：已完成数加粗突出，分母与百分比次级色 -->
      <span class="stage-nums" :class="{ 'is-done': progressPercent === 100 }">
        <b>{{ keyframeShots.length }}</b><span class="sep">/</span>{{ allShots.length }}
        <span class="pct">{{ progressPercent }}%</span>
      </span>
      <n-progress
        class="stage-progress"
        type="line"
        :percentage="progressPercent"
        :height="6"
        :border-radius="3"
        :show-indicator="false"
        :color="allShots.length > 0 && keyframeShots.length === allShots.length ? 'var(--state-success)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle)"
      />
      <span v-if="keyframeProgress.data.value?.inflight_queued" class="gen-state queued">
        排队 {{ keyframeProgress.data.value.inflight_queued }}
      </span>
      <span v-if="keyframeProgress.data.value?.inflight_running" class="gen-state running">
        生成中 {{ keyframeProgress.data.value.inflight_running }}
      </span>
      <span v-if="failedByShot.size" class="gen-state fail">
        最近失败 {{ failedByShot.size }} 张
        <n-button size="tiny" type="warning" :loading="retryingFailed" @click="retryFailedShots">重试</n-button>
      </span>
      <span v-if="keyframeProgress.error.value" class="gen-state fail">{{ keyframeProgress.error.value }}</span>


      <template #actions>
        <n-button size="small" quaternary :loading="keyframeProgress.loading.value" @click="keyframeProgress.refresh()">
          刷新进度
        </n-button>
        <n-button size="small" quaternary @click="refreshAll">刷新镜头</n-button>
        <!-- 跳转按钮不禁用：未完成时点击会展示缺哪些镜头，禁用则点不到这条提示 -->
        <n-button
          size="small"
          type="primary"
          :loading="stageTransition.loading.value"
          :title="allStoryboardImagesReady ? '进入分镜看板生成视频' : '点击查看还差哪些镜头'"
          @click="stageTransition.request()"
        >
          去分镜看板 <n-icon :component="ArrowForwardOutline" :size="14" />
        </n-button>
      </template>
    </StageCard>

    <!-- 卡二：批量出图（工作流 / 全选 / 状态筛选 / 批量生成与审核） -->
    <BatchToolbar
      :total="allShots.length"
      :selected="selectedList.length"
      :all-selected="allSelected"
      :batch-loading="batchMutation.isPending.value"
      :inflight-count="keyframeProgress.data.value?.inflight_count ?? 0"
      :cancel-loading="cancellingInflight"
      :workflow-id="generateWorkflowId"
      @update:workflow-id="(v: string | undefined) => (generateWorkflowId = v)"
      @toggle-all="toggleSelectAll"
      @generate="handleBatchGenerate"
      @cancel-inflight="handleCancelInflight"
    />

    <!-- 依赖预检：手动触发，只读展示所选镜头是否受上游变化影响 -->
    <div class="impact-precheck">
      <n-button size="tiny" quaternary :loading="impactChecking" @click="runImpactCheck">
        依赖预检
      </n-button>
      <span
        v-if="impactNote"
        class="impact-precheck-text"
        :class="{ 'is-warn': selectedImpactShots.length > 0 }"
      >
        {{ impactNote }}
        <template v-if="impactCheckedAt">（{{ relativeTime(impactCheckedAt) }}检查）</template>
      </span>
      <span v-else-if="impactError" class="impact-precheck-text is-error">
        依赖预检失败：{{ impactError }}
      </span>
    </div>

    <ErrorRetry
      v-if="scenesQuery.isError.value"
      title="场景加载失败"
      message="分镜图列表依赖场景与镜头数据，请重试；若持续失败请检查后端服务。"
      @retry="scenesQuery.refetch()"
    />

    <SkeletonList v-else-if="loading" variant="card" :rows="8" :columns="4" height="232px" />

    <EmptyState
      v-else-if="!scenesQuery.data.value?.items?.length"
      title="暂无场景"
      description="解析剧本并确认场景后，镜头会按场景列在这里。"
      action-text="前往解析页"
      @action="router.push(`/project/${projectId}/parse`)"
    />

    <div v-else class="scene-list">
      <section v-for="g in sceneGroups" :key="g.scene.id" class="scene-group">
        <header class="scene-header">
          <span class="scene-seq">#{{ g.scene.seq }}</span>
          <span class="scene-title">{{ g.scene.title }}</span>
          <span class="scene-count">{{ g.shots.length }} 镜</span>
        </header>
        <div class="shots-grid" :class="{ portrait: isPortrait }" :style="{ '--sb-thumb-ratio': thumbRatio }">
          <ShotCard
            v-for="shot in g.shots"
            :key="shot.id"
            :shot="shot"
            :seq="shotGlobalSeq(shot)"
            :selected="!!selectedIds[shot.id]"
            :inflight-text="inflightTextOf(shot)"
            :failed="failedByShot.has(shot.id)"
            :failed-message="failedByShot.get(shot.id) ?? ''"
            :busy="busyShot === shot.id || pendingShotId === shot.id"
            :prompt="promptDraft[shot.id] ?? shot.image_prompt ?? ''"
            @select="(v) => toggleSelect(shot, v)"
            @open="openDetail(shot)"
            @generate="handleGenerate(shot)"
            @replace="pickReplace(shot)"
          />
        </div>
      </section>
    </div>

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

    <!-- 镜头详情：大图 + 参考图 + 重新生成工作台 -->
    <ShotDetailDialog
      ref="dialogRef"
      :project-id="projectId"
      :scenes="scenesLite"
      :seq-of="shotGlobalSeq"
      @submitted="trackAfterSubmit"
      @first-generate="onFirstGenerate"
      @replace="pickReplace"
    />
  </div>
</template>

<style scoped>
.sb-images-page {
  min-height: calc(100vh - var(--app-header-height, 52px));
}

/* 批量出图卡下方的依赖预检行：默认无结果时只有按钮，避免常驻噪音 */
.impact-precheck {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  padding: 0 var(--sp-1);
  margin-top: calc(var(--sp-2) * -1);
}
.impact-precheck-text {
  font-size: 12px;
  color: var(--text-tertiary);
}
.impact-precheck-text.is-warn {
  color: var(--state-warning);
}
.impact-precheck-text.is-error {
  color: var(--danger-text);
}

/* ===== 执行进度卡内容（容器样式由 StageCard 提供） ===== */
/* 集数 chip：与分镜看板同一套视觉，多集项目靠它切换当前集 */
.episode-chip {
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  border-radius: var(--radius);
  padding: 6px var(--sp-4);
  font-size: 14px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: border-color var(--transition-fast), color var(--transition-fast);
}
.episode-chip:hover { border-color: var(--brand-primary); color: var(--brand-primary); }
.episode-chip.active {
  background: var(--brand-soft);
  border-color: var(--brand-primary);
  color: var(--brand-primary);
  font-weight: 600;
}
.gen-sep {
  width: 1px;
  height: 16px;
  background: var(--border-subtle);
  flex: none;
}
/* 进度数字（.stage-nums）与进度条（.stage-progress）的样式已收进 global.css，
 * 三页「执行进度」卡共用同一份口径。这里曾写 .gen-bar { flex: 1 } 与
 * .gen-nums { font-size: 12px }，与资产库的 4px 条 / 13px 数字不一致。 */
.gen-sub {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
  font-size: 12px;
}
.gen-state {
  color: var(--text-secondary);
}
.gen-state.queued {
  color: var(--accent-amber);
}
.gen-state.running {
  color: var(--brand-primary);
}
.gen-state.ok {
  color: var(--state-success);
  font-weight: 500;
}
.gen-state.fail {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--danger);
}

/* ===== 场景分组 + 镜头网格 ===== */
.scene-group {
  margin-bottom: var(--sp-6);
}
.scene-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  flex-wrap: wrap;
}
/* 场景名后接一条渐隐细线，作为唯一的分组记号，不额外加图标 */
.scene-header::after {
  content: '';
  flex: 1;
  min-width: var(--sp-6);
  height: 1px;
  background: linear-gradient(to right, var(--border-subtle), transparent);
}
.scene-seq {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-tertiary);
}
.scene-title {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.005em;
  color: var(--text-primary);
}
.scene-count {
  font-size: 12px;
  color: var(--text-tertiary);
}

/* 宽屏 4 列 → 1440 以下 2 列 → 900 以下单列；竖屏项目用窄列避免卡片过高，
   但列宽下限 196px：卡底「重新生成 / 替换上传 / ⋯」三个按钮不能再被截断 */
.shots-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--sp-4);
}
.shots-grid.portrait {
  grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
}
@media (max-width: 1100px) {
  .shots-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 768px) {
  /* 窄屏双列，不是单列：分镜图是 9:16 竖图，390 视口单列时卡宽 358、
     图片高 633、单卡 743px，26 个镜头页面总高 19942px —— 要滑 24 屏才能看完。
     双列后卡宽 171、图高 302、单卡约 410px，一屏可见 2 个镜头，
     且保留「同场景内横向比对」这个分镜评审的核心动作。
     卡底两个按钮在 171px 内放得下：「重新生成」是 flex:1 + ellipsis，
     「替换上传」固定约 76px，合计 171 − 16(padding) − 4(gap) = 151 ≥ 76 + 可压缩主按钮。 */
  .shots-grid,
  .shots-grid.portrait {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--sp-3);
  }
}

.hidden-file {
  display: none;
}
</style>
