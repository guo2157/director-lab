<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NSpace, NSpin, NEmpty, NTag, NTooltip, NInput, NCheckbox, NImage, NDrawer, NDrawerContent, NProgress, useMessage } from 'naive-ui'
import { listScenes, listSceneShots, listCharacters, listProps, confirmShotKeyframe, unconfirmShotKeyframe } from '@/api/script'
import { fetchRegenerateConfig, type RegenerateRef, type RefSlotInfo } from '@/api/regeneration'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { listWorkflows } from '@/api/workflows'
import { lockAsset, unlockAsset, replaceAsset } from '@/api/assets'
import { getProject } from '@/api/projects'
import { getKeyframeGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { Shot } from '@/types/script'
import type { KeyframeGenerationJob, KeyframeGenerationProgress } from '@/types/generation'
import type { GenerateShotPayload } from '@/types/job'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const projectId = computed(() => route.params.id as string)

// 资产审核状态：决定是否放行分镜图生成
const projectQuery = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
})
const reviewApproved = computed(() => (projectQuery.data.value?.assets_review_status ?? 'pending') === 'approved')
const reviewRejected = computed(() => projectQuery.data.value?.assets_review_status === 'rejected')

const scenesQuery = useQuery({
  queryKey: ['scenes', projectId],
  queryFn: () => listScenes(projectId.value, { include_shots: false }),
})

const shotsMap = ref<Record<string, Shot[]>>({})
const loadingShots = ref<Record<string, boolean>>({})
const promptDraft = ref<Record<string, string>>({})

/** 全量拉取场景内镜头：进度统计必须覆盖全场镜头，不能只看第一页（单页上限 100） */
async function loadSceneShots(sceneId: string, episode: number) {
  loadingShots.value[sceneId] = true
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
    shotsMap.value[sceneId] = all
  } catch (e: any) {
    message.error(`加载镜头失败: ${e?.message ?? e}`)
  } finally {
    loadingShots.value[sceneId] = false
  }
}

watch(
  () => scenesQuery.data.value?.items ?? [],
  (scenes) => {
    for (const sc of scenes) {
      const episode = sc.episodes?.[0] ?? 1
      if (!shotsMap.value[sc.id]) void loadSceneShots(sc.id, episode)
    }
  },
  { immediate: true },
)

const allShots = computed(() => Object.values(shotsMap.value).flat())
// --- 全片连续镜头序号 ---
// shot.seq 是场景内序号，各场景都会从 1 开始；按场景 seq 升序 + 场景内镜头 seq 升序累加得到 1..N 的全片镜号。
const orderedShots = computed<Shot[]>(() => {
  const scenes = [...(scenesQuery.data.value?.items ?? [])].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
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
const keyframeShots = computed(() => allShots.value.filter((s) => !!s.current_assets?.keyframe))
const confirmedShots = computed(() => allShots.value.filter((s) => s.keyframe_confirmed))
const allConfirmed = computed(
  () => allShots.value.length > 0 && allShots.value.every((s) => !!s.current_assets?.keyframe && s.keyframe_confirmed),
)

// --- 生成进度（后端聚合：整体 X/Y、正在生成哪几镜、是否全部完成） ---
const keyframeProgress = useGenerationProgress<KeyframeGenerationProgress>({
  fetcher: () => getKeyframeGenerationProgress(projectId.value),
  onChange: () => {
    // 出图完成后立即刷新镜头，让分镜图缩略图与状态同步
    refreshAll()
  },
  onAllComplete: () => {
    message.success('全部分镜图已生成完成，请逐镜确认后进入分镜看板')
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

/** 在途任务涉及的场景名（用于提示「哪些场景正在出图」） */
const inflightSceneNames = computed(
  () => [...new Set((keyframeProgress.data.value?.inflight ?? []).map((j) => j.scene_title).filter(Boolean))] as string[],
)

/** 顶部进度条数值：已出图 / 全部镜头 */
const progressPercent = computed(() => {
  const total = allShots.value.length
  if (!total) return 0
  return Math.round((keyframeShots.value.length / total) * 100)
})

const genHeadline = computed(() => {
  const p = keyframeProgress.data.value
  const total = allShots.value.length
  const done = keyframeShots.value.length
  const inflight = p?.inflight_count ?? 0
  if (inflight > 0) {
    const scenes = inflightSceneNames.value.slice(0, 3).join('、')
    return `正在生成 ${inflight} 张分镜图${scenes ? `（${scenes}${inflightSceneNames.value.length > 3 ? ' 等' : ''}）` : ''}`
  }
  if (total > 0 && done === total) return '全部分镜图已生成完成'
  if (!total) return '暂无镜头'
  return `还有 ${total - done} 张分镜图未生成`
})

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

/** 失败镜头重试：仅重提当前失败的镜头 */
const retryingFailed = ref(false)
async function retryFailedShots() {
  const ids = [...failedByShot.value.keys()]
  if (!ids.length) return
  retryingFailed.value = true
  try {
    await generateProjectShots(projectId.value, { shot_ids: ids, target: 'keyframe' })
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

const generateMutation = useMutation({
  mutationFn: (payload: { shotId: string; params?: Record<string, string> }) =>
    generateShot(payload.shotId, { target: 'keyframe', params: payload.params } as GenerateShotPayload),
  onSuccess: async () => {
    message.success('分镜图生成任务已提交，正在生成（进度见顶部进度条）')
    // 提交后立即建立在途基线并轮询，全部结束后提示「全部分镜图已生成完成」
    await trackAfterSubmit()
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})

function handleGenerate(shot: Shot) {
  const params: Record<string, string> = {}
  if (promptDraft.value[shot.id]) params.prompt = promptDraft.value[shot.id]
  generateMutation.mutate({ shotId: shot.id, params })
}

function onPromptInput(shot: Shot, value: string) {
  promptDraft.value[shot.id] = value
}

// 勾选与批量出图
const selectedIds = ref<Record<string, boolean>>({})
const selectedList = computed(() => allShots.value.filter((s) => selectedIds.value[s.id]))
const allSelected = computed(() => allShots.value.length > 0 && selectedList.value.length === allShots.value.length)
const busyShot = ref<string>('')

function toggleSelect(shot: Shot) {
  selectedIds.value[shot.id] = !selectedIds.value[shot.id]
}

function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = {}
    return
  }
  const next: Record<string, boolean> = {}
  for (const s of allShots.value) next[s.id] = true
  selectedIds.value = next
}

function refreshAll() {
  for (const sc of scenesQuery.data.value?.items ?? []) {
    void loadSceneShots(sc.id, sc.episodes?.[0] ?? 1)
  }
}

const batchMutation = useMutation({
  mutationFn: (ids: string[]) => generateProjectShots(projectId.value, { shot_ids: ids, target: 'keyframe' }),
  onSuccess: async (res: any) => {
    message.success(`已提交 ${res?.count ?? 0} 个分镜图任务，正在生成（进度见顶部进度条）`)
    await trackAfterSubmit()
  },
  onError: (e: any) => message.error(`批量生成失败: ${e?.message ?? e}`),
})

function handleBatchGenerate() {
  if (!reviewApproved.value) {
    message.warning('公共资产尚未通过人工审核，暂不可生成分镜图')
    return
  }
  const picked = selectedList.value
  if (!picked.length) {
    message.warning('请先勾选需要生成分镜图的镜头')
    return
  }
  const locked = picked.filter((s) => s.current_assets?.keyframe?.locked)
  if (locked.length) {
    message.warning(`已勾选镜头中有 ${locked.length} 个分镜图已锁定，请先解锁后再批量生成`)
    return
  }
  void submitByPlans(picked)
}

/** 按已定稿定义批量提交：名称与提示词以库中定义为准；定义缺失时回退镜头级批量 */
async function submitByPlans(picked: Shot[]) {
  try {
    const res = await listAssetPlans(projectId.value, { plan_type: 'keyframe' })
    const pickedIds = new Set(picked.map((s) => s.id))
    const planIds = (res.items || [])
      .filter((p) => p.owner_id && pickedIds.has(p.owner_id))
      .map((p) => p.id)
    if (!planIds.length) {
      batchMutation.mutate(picked.map((s) => s.id))
      return
    }
    const gen = await generateFromPlans(projectId.value, { plan_type: 'keyframe', plan_ids: planIds })
    message.success(`已按定义提交 ${gen.count} 个分镜图任务，正在生成（进度见顶部进度条）`)
    if (gen.skipped?.length) {
      const reasons = [...new Set(gen.skipped.map((s) => s.reason))].join('、')
      message.info(`另有 ${gen.skipped.length} 条定义被跳过：${reasons}`)
    }
    await trackAfterSubmit()
  } catch (e: any) {
    message.error(`批量生成失败: ${e?.message ?? e}`)
  }
}

// 锁定与解锁：锁定的分镜图不会被重绘覆盖
async function handleToggleLock(shot: Shot) {
  const kf = shot.current_assets?.keyframe
  if (!kf?.asset_id) {
    message.warning('该镜暂无可锁定的分镜图')
    return
  }
  busyShot.value = shot.id
  try {
    if (kf.locked) {
      await unlockAsset(kf.asset_id)
      message.success('已解锁，该镜分镜图可重新生成或替换')
    } else {
      await lockAsset(kf.asset_id)
      message.success('已锁定，该镜分镜图不会被重新生成覆盖')
    }
    refreshAll()
  } catch (e: any) {
    message.error(`操作失败: ${e?.message ?? e}`)
  } finally {
    busyShot.value = ''
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
  if (kf.locked) {
    message.warning('该镜分镜图已锁定，请先解锁再替换')
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
    refreshAll()
  } catch (err: any) {
    message.error(`替换失败: ${err?.message ?? err}`)
  } finally {
    busyShot.value = ''
  }
}

async function handleConfirm(shot: Shot, sceneId: string, episode: number) {
  try {
    await confirmShotKeyframe(shot.id)
    message.success('已确认该镜分镜图')
    await loadSceneShots(sceneId, episode)
  } catch (e: any) {
    message.error(`确认失败: ${e?.message ?? e}`)
  }
}

async function handleUnconfirm(shot: Shot, sceneId: string, episode: number) {
  try {
    await unconfirmShotKeyframe(shot.id)
    message.success('已取消确认，可重新生成或重新审核')
    await loadSceneShots(sceneId, episode)
  } catch (e: any) {
    message.error(`取消失败: ${e?.message ?? e}`)
  }
}
// 批量审核通过：对勾选镜头中已出图且未确认的逐一执行确认（复用单镜确认接口）
const batchConfirming = ref(false)
async function handleBatchConfirm() {
  const targets = selectedList.value.filter((s) => !!s.current_assets?.keyframe && !s.keyframe_confirmed)
  if (!targets.length) {
    message.warning('勾选镜头中没有待确认的分镜图')
    return
  }
  batchConfirming.value = true
  let ok = 0
  const failed: string[] = []
  for (const shot of targets) {
    try {
      await confirmShotKeyframe(shot.id)
      ok++
    } catch {
      failed.push(shot.title || shot.id)
    }
  }
  batchConfirming.value = false
  if (ok) message.success(`批量审核通过 ${ok} 个分镜图`)
  if (failed.length) message.error(`审核失败 ${failed.length} 个：${failed.join('、')}`)
  refreshAll()
}

// --- 镜头详情：分镜图与生成时的参考图 ---
// 生图（图生图）工作流的参考图能力以工作流声明的 reference_slots 为唯一事实来源，
// 不在前端写死槽位数量与槽位名：多参考版声明 9 槽，纯文生图版声明 0 槽。
// 有分镜图时取该资产实际工作流的 available_slots；尚无分镜图时取默认生图工作流的声明。
const IMG_RESERVED_SLOTS = new Set(['ref_image', 'ref_prev_frame', 'first_frame', 'end_frame'])

function isCharSlot(param: string): boolean {
  return !IMG_RESERVED_SLOTS.has(param)
    && (param.startsWith('ref_identity') || param.startsWith('ref_character') || param.startsWith('ref_char'))
}

function isSceneSlot(param: string): boolean {
  return param.startsWith('ref_scene')
}

function isPropSlot(param: string): boolean {
  return param.startsWith('ref_prop')
}

/** 槽位展示名：角色槽按声明顺序编号，场景/道具按类型命名，未知槽位回落参数名。 */
function imageSlotLabel(param: string, charIndex: number, propIndex: number): string {
  if (isCharSlot(param)) return charIndex === 0 ? '角色身份参考' : `角色参考 ${charIndex + 1}`
  if (isSceneSlot(param)) return '场景基准图'
  if (isPropSlot(param)) return propIndex === 0 ? '道具参考' : `道具参考 ${propIndex + 1}`
  return param
}

/** 归一化工作流 reference_slots（兼容字符串数组与对象数组两种声明形式）。 */
function normalizeRefSlots(caps: Record<string, unknown> | null | undefined): RefSlotInfo[] {
  const raw = Array.isArray(caps?.reference_slots) ? (caps!.reference_slots as unknown[]) : []
  const slots: RefSlotInfo[] = []
  for (const item of raw) {
    if (typeof item === 'string') {
      if (item) slots.push({ param: item, input_key: null, required: false, semantic: 'reference', role: null, system: IMG_RESERVED_SLOTS.has(item) })
    } else if (item && typeof item === 'object') {
      const o = item as Record<string, unknown>
      const param = typeof o.param === 'string' ? o.param : ''
      if (!param) continue
      slots.push({
        param,
        input_key: typeof o.input_key === 'string' ? o.input_key : null,
        required: o.optional === false,
        semantic: 'reference',
        role: null,
        system: IMG_RESERVED_SLOTS.has(param),
      })
    }
  }
  return slots
}

/** 无分镜图时的槽位来源：复刻后端 _match_workflow 的分镜图选型（默认 → 内置 → 带参考槽位）。 */
const imageWorkflowsQuery = useQuery({
  queryKey: ['workflows', 'image', 'storyboard'],
  queryFn: () => listWorkflows({ workflow_type: 'image' }),
})
const fallbackImageSlots = computed<RefSlotInfo[]>(() => {
  const items = (imageWorkflowsQuery.data.value?.items ?? []) as Array<{
    is_default?: boolean
    is_builtin?: boolean
    capabilities?: Record<string, unknown> | null
  }>
  if (!items.length) return []
  const pick = items.find((w) => w.is_default) ?? items.find((w) => w.is_builtin) ?? items[0]
  const slots = normalizeRefSlots(pick.capabilities)
  // 分镜图会改用带参考槽位的工作流：默认工作流 0 槽时切到声明角色槽位的那一个
  if (!slots.some((s) => !s.system)) {
    for (const w of items) {
      const alt = normalizeRefSlots(w.capabilities)
      if (alt.some((s) => isCharSlot(s.param))) return alt
    }
  }
  return slots
})

/** 该资产实际工作流的槽位；为空时回落到默认生图工作流声明。 */
const kfActualSlots = ref<RefSlotInfo[]>([])
const kfSlots = computed<RefSlotInfo[]>(() => {
  const actual = kfActualSlots.value.filter((s) => !s.system)
  return actual.length ? actual : fallbackImageSlots.value.filter((s) => !s.system)
})
const kfSlotMeta = computed<Record<string, { label: string; inputKey: string }>>(() => {
  const map: Record<string, { label: string; inputKey: string }> = {}
  let charIndex = 0
  let propIndex = 0
  for (const s of kfSlots.value) {
    map[s.param] = {
      label: imageSlotLabel(s.param, isCharSlot(s.param) ? charIndex++ : 0, isPropSlot(s.param) ? propIndex++ : 0),
      inputKey: s.input_key || '—',
    }
  }
  return map
})
const kfMaxRefs = computed(() => kfSlots.value.length)
const kfCharSlots = computed(() => kfSlots.value.filter((s) => isCharSlot(s.param)).map((s) => s.param))
const kfPropSlots = computed(() => kfSlots.value.filter((s) => isPropSlot(s.param)).map((s) => s.param))
const kfSceneSlot = computed(() => kfSlots.value.find((s) => isSceneSlot(s.param))?.param ?? '')

interface KeyframeRefItem {
  key: string
  slot: string
  name: string
  roleLabel: string
  inputKey: string
  assetId: string
  url: string
  required?: boolean
  /** 真实入参：来自重生成任务记录的实际参考图 */
  injected?: boolean
  /** 对应定妆图 / 基准图尚未生成 */
  assetMissing?: boolean
  /** 当前生图工作流无可用槽位，本次不会传入 */
  noSlot?: boolean
}

const detailVisible = ref(false)
const detailShot = ref<Shot | null>(null)
const kfRefs = ref<RegenerateRef[]>([])
/** 首次生成的分镜图没有独立的参考图记录，真实传参保存在任务输入快照的参数里，
 *  形如 { ref_identity: 'ast_xxx_v1.png', ref_scene: 'ast_yyy_v4.png' }，
 *  这里从中还原出当时实际传入的槽位与资产，作为真实入参展示。 */
interface KfInjectedRef { slot: string; assetId: string; filename: string }
const kfInjectedRefs = ref<KfInjectedRef[]>([])
const kfRefsLoading = ref(false)
const kfNegativeSupported = ref(false)

const charsQuery = useQuery({
  queryKey: computed(() => ['characters', projectId.value]),
  queryFn: () => listCharacters(projectId.value),
})
const propsQuery = useQuery({
  queryKey: computed(() => ['props', projectId.value]),
  queryFn: () => listProps(projectId.value),
})

function openDetail(shot: Shot) {
  detailShot.value = shot
  detailVisible.value = true
  void loadKeyframeRefs(shot)
}

async function loadKeyframeRefs(shot: Shot) {
  kfRefs.value = []
  kfInjectedRefs.value = []
  kfActualSlots.value = []
  kfNegativeSupported.value = false
  if (!shot.current_assets?.keyframe) return
  kfRefsLoading.value = true
  try {
    const cfg = await fetchRegenerateConfig(shot.id, 'keyframe')
    kfActualSlots.value = cfg.available_slots ?? []
    kfRefs.value = (cfg.refs ?? []).filter((r) => r.enabled !== false)
    kfNegativeSupported.value = Boolean(cfg.negative_prompt?.supported)
    // 快照参数里的 ref_* 键即生成时实际传入的槽位，值为对应资产文件名
    const injected: KfInjectedRef[] = []
    for (const [key, value] of Object.entries(cfg.model_params ?? {})) {
      if (!key.startsWith('ref_') || typeof value !== 'string') continue
      const m = value.match(/(ast_[a-z0-9]+)/i)
      if (!m) continue
      injected.push({ slot: key, assetId: m[1], filename: value })
    }
    kfInjectedRefs.value = injected
  } catch {
    // 配置读取失败时静默降级为槽位推导，不打断详情查看
    kfRefs.value = []
    kfInjectedRefs.value = []
  } finally {
    kfRefsLoading.value = false
  }
}

/** 参考图列表：真实入参优先，其余按工作流槽位规则推导并明确标注来源 */
const kfRefList = computed<KeyframeRefItem[]>(() => {
  const shot = detailShot.value
  if (!shot) return []
  const chars = (charsQuery.data.value?.items ?? []) as Array<{ id: string; name: string; portrait_asset_id?: string | null }>
  const scenes = (scenesQuery.data.value?.items ?? []) as Array<{ id: string; title: string; baseline_asset_id?: string | null }>
  const props = (propsQuery.data.value?.items ?? []) as Array<{ id: string; name: string; anchor_asset_id?: string | null }>

  const items: KeyframeRefItem[] = []
  const seenAsset = new Set<string>()
  const push = (v: KeyframeRefItem) => {
    if (v.assetId) {
      if (seenAsset.has(v.assetId)) return
      seenAsset.add(v.assetId)
    }
    if (items.some((x) => x.key === v.key)) return
    items.push(v)
  }

  const nameOfAsset = (assetId: string, slot: string): string => {
    const c = chars.find((x) => x.portrait_asset_id === assetId)
    if (c) return c.name
    const s = scenes.find((x) => x.baseline_asset_id === assetId)
    if (s) return s.title
    const p = props.find((x) => x.anchor_asset_id === assetId)
    if (p) return p.name
    return kfSlotMeta.value[slot]?.label ?? slot
  }

  // 一（甲）、首次生成的真实传参：从任务输入快照参数还原
  for (const r of kfInjectedRefs.value) {
    const meta = kfSlotMeta.value[r.slot]
    const named = nameOfAsset(r.assetId, r.slot)
    push({
      key: `${r.slot}-${r.assetId}`,
      slot: r.slot,
      // 资产已不在项目列表时回落显示文件名，避免只看到槽位名而认不出是哪张图
      name: named === (meta?.label ?? r.slot) ? r.filename : named,
      roleLabel: meta?.label ?? r.slot,
      inputKey: meta?.inputKey ?? '—',
      assetId: r.assetId,
      url: `/api/assets/${r.assetId}/file`,
      required: r.slot === 'ref_identity',
      injected: true,
    })
  }

  // 一（乙）、真实入参：该镜头重生成任务里记录的图排最前
  for (const r of kfRefs.value) {
    if (!r.asset_id) continue
    const slot = r.slot_param
    const meta = kfSlotMeta.value[slot]
    const assetId = r.asset_id as string
    push({
      key: `${slot}-${assetId}`,
      slot,
      name: nameOfAsset(assetId, slot),
      roleLabel: meta?.label ?? r.role ?? slot,
      inputKey: r.input_key || meta?.inputKey || '—',
      assetId,
      url: `/api/assets/${assetId}/file`,
      required: Boolean(r.required),
      injected: true,
    })
  }

  // 提示词中的 @引用（与后端注入顺序一致）
  const promptText = `${shot.image_prompt ?? ''} ${shot.description ?? ''}`
  const atNames = new Set<string>()
  let atIdx = promptText.indexOf('@')
  while (atIdx !== -1) {
    const rest = promptText.slice(atIdx + 1)
    const m = rest.match(/^[^\s，,。.;；:：@]+/)
    if (m) atNames.add(m[0])
    atIdx = promptText.indexOf('@', atIdx + 1)
  }

  // 二、角色：本镜主角色 + @引用角色，按工作流声明的角色槽位顺序依次填充
  const charSlots = kfCharSlots.value
  const relatedChars = chars
    .filter((c) => shot.character_id === c.id || atNames.has(c.name))
    .sort((a, b) => (a.id === shot.character_id ? -1 : b.id === shot.character_id ? 1 : 0))
  relatedChars.forEach((c, idx) => {
    const slot = charSlots[idx] ?? ''
    const assetId = c.portrait_asset_id ?? ''
    push({
      key: `char-${c.id}`,
      slot,
      name: c.name,
      roleLabel: slot ? kfSlotMeta.value[slot].label : '角色参考',
      inputKey: slot ? kfSlotMeta.value[slot].inputKey : '—',
      assetId,
      url: assetId ? `/api/assets/${assetId}/file` : '',
      required: slot === 'ref_identity',
      assetMissing: !assetId,
      noSlot: !slot,
    })
  })

  // 三、场景：所属场景优先占用工作流声明的场景槽位，其余无槽位
  const sceneSlot = kfSceneSlot.value
  const relatedScenes = scenes
    .filter((s) => shot.scene_id === s.id || atNames.has(s.title))
    .sort((a, b) => (a.id === shot.scene_id ? -1 : b.id === shot.scene_id ? 1 : 0))
  relatedScenes.forEach((s, idx) => {
    const slot = idx === 0 ? sceneSlot : ''
    const assetId = s.baseline_asset_id ?? ''
    push({
      key: `scene-${s.id}`,
      slot,
      name: s.title,
      roleLabel: slot ? kfSlotMeta.value[slot].label : '场景参考',
      inputKey: slot ? kfSlotMeta.value[slot].inputKey : '—',
      assetId,
      url: assetId ? `/api/assets/${assetId}/file` : '',
      assetMissing: !assetId,
      noSlot: !slot,
    })
  })

  // 四、道具：按工作流声明的道具槽位顺序填充，未声明的照列但不参与传入
  const propSlots = kfPropSlots.value
  const relatedProps = props.filter((p) => atNames.has(p.name))
  relatedProps.forEach((p, idx) => {
    const slot = propSlots[idx] ?? ''
    const assetId = p.anchor_asset_id ?? ''
    push({
      key: `prop-${p.id}`,
      slot,
      name: p.name,
      roleLabel: slot ? kfSlotMeta.value[slot].label : '道具参考',
      inputKey: slot ? kfSlotMeta.value[slot].inputKey : '—',
      assetId,
      url: assetId ? `/api/assets/${assetId}/file` : '',
      assetMissing: !assetId,
      noSlot: !slot,
    })
  })

  return items
})

function statusOf(shot: Shot): { label: string; type: 'default' | 'warning' | 'success' } {
  if (!shot.current_assets?.keyframe) return { label: '未生成', type: 'default' }
  if (!shot.keyframe_confirmed) return { label: '待人工确认', type: 'warning' }
  return { label: '已确认', type: 'success' }
}

</script>

<template>
  <div class="sb-images-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">分镜图</h1>
        <span class="page-hint">基于已审核的公共资产生成分镜图，逐镜确认后进入分镜看板</span>
      </div>
    </header>
    <div class="gate-banner" :class="{ ok: reviewApproved, bad: reviewRejected }">
      <span class="gate-badge">{{ reviewApproved ? '✓' : '⏳' }}</span>
      <span
        class="status-tag"
        :class="reviewApproved ? 'is-succeeded' : reviewRejected ? 'is-failed' : 'is-pending'"
      >{{ reviewApproved ? '审核已通过' : reviewRejected ? '审核已驳回' : '待审核' }}</span>
      <span class="gate-text">
        {{ reviewApproved
          ? '公共资产已通过人工审核，可生成分镜图；分镜图逐镜确认无误后进入分镜看板生成视频'
          : reviewRejected
            ? '公共资产审核被驳回：请返回资产库修改或替换资产后重新提交审核'
            : '公共资产尚未通过人工审核：请先在资产库完成审核，才能生成分镜图' }}
      </span>
      <n-button size="small" quaternary @click="router.push(`/project/${projectId}/assets`)">去资产库</n-button>
      <n-button size="small" quaternary @click="scenesQuery.refetch()">刷新</n-button>
      <n-button
        size="small"
        type="primary"
        secondary
        :disabled="!selectedList.length"
        :loading="batchConfirming"
        @click="handleBatchConfirm"
      >批量审核通过（{{ selectedList.length }}）</n-button>
      <n-button size="small" type="primary" :disabled="!allConfirmed" @click="router.push(`/project/${projectId}/storyboard`)">
        去分镜看板
      </n-button>
    </div>

    <input ref="fileInputRef" type="file" accept="image/*" class="hidden-file" @change="onReplaceFileChange" />

    <!-- 生成总体进度：全片 X/Y、正在生成哪几镜、失败项、是否全部完成 -->
    <div class="gen-panel" :class="{ done: allShots.length > 0 && keyframeShots.length === allShots.length && !keyframeProgress.hasInflight.value, fail: failedByShot.size > 0 }">
      <div class="gen-head">
        <span class="gen-badge">{{ keyframeProgress.hasInflight.value ? '⏳' : allShots.length > 0 && keyframeShots.length === allShots.length ? '✓' : '•' }}</span>
        <span class="gen-title">{{ genHeadline }}</span>
        <span class="gen-nums">
          分镜图 {{ keyframeShots.length }}/{{ allShots.length }}（{{ progressPercent }}%）· 已确认 {{ confirmedShots.length }}
        </span>
        <n-button size="tiny" quaternary :loading="keyframeProgress.loading.value" @click="keyframeProgress.refresh()">刷新进度</n-button>
        <n-button size="tiny" quaternary @click="refreshAll">刷新镜头</n-button>
      </div>
      <n-progress
        type="line"
        :percentage="progressPercent"
        :height="8"
        :border-radius="4"
        :show-indicator="false"
        :color="allShots.length > 0 && keyframeShots.length === allShots.length ? 'var(--state-success, #22c55e)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle, #e5e7eb)"
        class="gen-bar"
      />
      <div class="gen-sub">
        <span v-if="keyframeProgress.data.value && keyframeProgress.data.value.inflight_queued" class="gen-state queued">
          排队 {{ keyframeProgress.data.value.inflight_queued }}
        </span>
        <span v-if="keyframeProgress.data.value && keyframeProgress.data.value.inflight_running" class="gen-state running">
          生成中 {{ keyframeProgress.data.value.inflight_running }}
        </span>
        <span v-if="allShots.length > 0 && keyframeShots.length === allShots.length" class="gen-state ok">
          全部分镜图已生成完成<template v-if="keyframeProgress.data.value?.last_completed_at">（最近完成于 {{ new Date(keyframeProgress.data.value.last_completed_at).toLocaleTimeString('zh-CN', { hour12: false }) }}）</template>
        </span>
        <span v-else class="gen-state">未生成 {{ allShots.length - keyframeShots.length }}</span>
        <span v-if="failedByShot.size" class="gen-state fail">
          最近失败 {{ failedByShot.size }} 张
          <n-button size="tiny" type="warning" :loading="retryingFailed" @click="retryFailedShots">重试失败镜头</n-button>
        </span>
        <span v-if="keyframeProgress.error.value" class="gen-state fail">{{ keyframeProgress.error.value }}</span>
      </div>
      <!-- 正在生成的镜头明细：场景 + 镜号 + 百分比 -->
      <div v-if="keyframeProgress.data.value?.inflight?.length" class="gen-inflight">
        <span v-for="job in keyframeProgress.data.value.inflight" :key="job.job_id" class="gen-inflight-item">
          <span class="gen-inflight-name">{{ job.scene_title || '未知场景' }} · 镜 {{ job.shot_seq ?? '—' }}</span>
          <span class="gen-inflight-state" :class="job.status">
            {{ job.status === 'running' ? (job.progress != null ? `生成中 ${job.progress}%` : '生成中') : job.status === 'queued' ? '排队中' : '提交确认中' }}
          </span>
        </span>
      </div>
    </div>

    <div class="summary-bar">
      <n-tag size="small" round>镜头 {{ allShots.length }}</n-tag>
      <n-tag size="small" round>已出图 {{ keyframeShots.length }}</n-tag>
      <n-tag size="small" round type="warning">待确认 {{ keyframeShots.length - confirmedShots.length }}</n-tag>
      <n-tag size="small" round type="success">已确认 {{ confirmedShots.length }}</n-tag>
      <span class="bar-gap" />
      <n-checkbox
        :checked="allSelected"
        :disabled="!allShots.length"
        @update:checked="toggleSelectAll"
      >全选</n-checkbox>
      <n-button
        size="medium"
        type="primary"
        :disabled="!reviewApproved || !selectedList.length"
        :loading="batchMutation.isPending.value"
        @click="handleBatchGenerate"
      >批量生成分镜图（{{ selectedList.length }}）</n-button>
    </div>

    <n-spin v-if="scenesQuery.isLoading.value" class="page-loading" />
    <div v-else-if="scenesQuery.isError.value" class="state-block">
      <p class="state-text">场景加载失败，请稍后重试</p>
      <n-button secondary @click="() => scenesQuery.refetch()">重试</n-button>
    </div>
    <n-empty v-else-if="!scenesQuery.data.value?.items?.length" description="暂无场景，请先完成剧本解析">
      <template #extra>
        <n-button type="primary" @click="router.push(`/project/${projectId}/parse`)">前往解析页</n-button>
      </template>
    </n-empty>

    <div v-else class="scenes-container">
      <div v-for="scene in scenesQuery.data.value.items" :key="scene.id" class="scene-group">
        <div class="scene-header">
          <span class="scene-seq">#{{ scene.seq }}</span>
          <span class="scene-title">{{ scene.title }}</span>
          <n-tag v-if="scene.episodes?.length" size="tiny" round>第 {{ scene.episodes.join(', ') }} 集</n-tag>
        </div>
        <div class="shots-grid">
          <n-spin v-if="loadingShots[scene.id]" size="small" />
          <template v-else>
            <div
              v-for="shot in shotsMap[scene.id] || []"
              :key="shot.id"
              class="shot-card"
              :class="{ 'is-generating': !!inflightByShot.get(shot.id), 'is-failed': failedByShot.has(shot.id) }"
            >
              <!-- 点击图片直接进入镜头详情（详情内含大图与参考图），因此关闭图片自带的大图预览 -->
              <div class="shot-thumb" @click="openDetail(shot)">
                <div class="thumb-overlay">
                  <div class="select-box" :class="{ checked: !!selectedIds[shot.id] }" @click.stop="toggleSelect(shot)">
                    <n-checkbox :checked="!!selectedIds[shot.id]" size="large" class="select-box-cb" />
                  </div>
                  <n-tag
                    v-if="shot.current_assets?.keyframe?.locked"
                    size="tiny"
                    type="warning"
                    @click.stop
                  >已锁定</n-tag>
                </div>
                <n-image
                  v-if="shot.current_assets?.keyframe?.thumb_url || shot.current_assets?.keyframe?.url"
                  class="shot-thumb-img"
                  :src="shot.current_assets?.keyframe?.thumb_url || shot.current_assets?.keyframe?.url || ''"
                  :preview-disabled="true"
                  object-fit="cover"
                  alt="分镜图"
                />
                <div v-else class="thumb-empty">
                  <template v-if="inflightByShot.get(shot.id)">
                    <span class="thumb-generating">{{ inflightTextOf(shot) }}</span>
                  </template>
                  <template v-else>未生成分镜图</template>
                </div>
              </div>
              <div class="shot-body">
                <div class="shot-head">
                  <span class="shot-name">{{ shot.title || `镜头 ${shotGlobalSeq(shot)}` }}</span>
                  <!-- 生成中优先展示排队/百分比，失败展示错误，其余走已确认/待确认状态 -->
                  <span
                    v-if="inflightByShot.get(shot.id)"
                    class="status-tag is-generating"
                  >{{ inflightTextOf(shot) }}</span>
                  <span
                    v-else-if="failedByShot.has(shot.id)"
                    class="status-tag is-failed"
                  >生成失败</span>
                  <span
                    v-else
                    class="status-tag"
                    :class="statusOf(shot).type === 'success' ? 'is-succeeded' : statusOf(shot).type === 'warning' ? 'is-pending' : 'is-stale'"
                  >{{ statusOf(shot).label }}</span>
                </div>
                <n-input
                  :value="promptDraft[shot.id] ?? shot.image_prompt ?? ''"
                  type="textarea"
                  :rows="2"
                  size="small"
                  placeholder="分镜图提示词"
                  @update:value="(v: string) => onPromptInput(shot, v)"
                />
                <div v-if="failedByShot.has(shot.id)" class="shot-error" :title="failedByShot.get(shot.id)">
                  失败原因：{{ failedByShot.get(shot.id) }}
                </div>
                <n-space :size="8" class="shot-actions">
                  <n-tooltip>
                    <template #trigger>
                      <n-button
                        size="small"
                        type="primary"
                        :disabled="!reviewApproved || !!shot.current_assets?.keyframe?.locked || !!inflightByShot.get(shot.id)"
                        :loading="generateMutation.isPending.value"
                        @click="handleGenerate(shot)"
                      >{{ inflightByShot.get(shot.id) ? inflightTextOf(shot) : shot.current_assets?.keyframe ? '重新生成' : '生成分镜图' }}</n-button>
                    </template>
                    {{ reviewApproved ? '提交分镜图生成任务' : '需先通过公共资产人工审核' }}
                  </n-tooltip>
                  <n-button
                    v-if="shot.current_assets?.keyframe && !shot.keyframe_confirmed"
                    size="small"
                    type="success"
                    @click="handleConfirm(shot, scene.id, scene.episodes?.[0] ?? 1)"
                  >确认分镜图</n-button>
                  <n-button
                    v-else-if="shot.keyframe_confirmed"
                    size="small"
                    quaternary
                    @click="handleUnconfirm(shot, scene.id, scene.episodes?.[0] ?? 1)"
                  >取消确认</n-button>
                  <n-button
                    v-if="shot.current_assets?.keyframe"
                    size="small"
                    secondary
                    :loading="busyShot === shot.id"
                    @click="handleToggleLock(shot)"
                  >{{ shot.current_assets.keyframe.locked ? '解锁' : '锁定' }}</n-button>
                  <n-button
                    v-if="shot.current_assets?.keyframe && !shot.current_assets.keyframe.locked"
                    size="small"
                    quaternary
                    :loading="busyShot === shot.id"
                    @click="pickReplace(shot)"
                  >替换上传</n-button>
                </n-space>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 镜头详情：分镜图与生成时的真实参考图 -->
    <n-drawer v-model:show="detailVisible" :width="'min(520px, 100vw)'">
      <n-drawer-content title="分镜图详情" closable>
        <template v-if="detailShot">
          <div class="detail-hero">
            <n-image
              v-if="detailShot.current_assets?.keyframe?.url || detailShot.current_assets?.keyframe?.thumb_url"
              :src="detailShot.current_assets?.keyframe?.url || detailShot.current_assets?.keyframe?.thumb_url || ''"
              :preview-src="detailShot.current_assets?.keyframe?.url || detailShot.current_assets?.keyframe?.thumb_url || ''"
              class="detail-img"
              object-fit="contain"
              alt="分镜图"
            />
            <div v-else class="detail-empty">该镜头尚未生成分镜图</div>
            <div class="detail-meta">
              <span class="detail-name">{{ detailShot.title || `镜头 ${shotGlobalSeq(detailShot)}` }}</span>
              <span class="detail-status">{{ statusOf(detailShot).label }}</span>
              <n-tag v-if="detailShot.current_assets?.keyframe?.locked" size="tiny" type="warning">已锁定</n-tag>
              <n-tag v-if="detailShot.keyframe_confirmed" size="tiny" type="success">已确认</n-tag>
            </div>
          </div>

          <!-- 分区一：生成参考图 -->
          <section class="detail-section">
            <div class="detail-section-head">
              <span class="detail-section-index">1</span>
              <span class="detail-section-name">生成参考图</span>
              <span class="detail-section-hint">当前生图工作流 {{ kfMaxRefs }} 个参考槽位</span>
            </div>
            <n-spin :show="kfRefsLoading">
              <div v-if="kfRefList.length" class="kf-ref-list">
                <div v-for="r in kfRefList" :key="r.key" class="kf-ref-item">
                  <n-image
                    v-if="r.url"
                    :src="r.url"
                    width="52"
                    height="52"
                    object-fit="cover"
                    class="kf-ref-thumb"
                    :img-props="{ style: 'width:52px;height:52px;object-fit:cover;border-radius:6px' }"
                  />
                  <div v-else class="kf-ref-thumb kf-ref-empty">未生成</div>
                  <div class="kf-ref-meta">
                    <div class="kf-ref-name">
                      {{ r.name }}
                      <span v-if="r.injected" class="kf-tag ok">真实入参</span>
                      <span v-else-if="r.assetMissing" class="kf-tag warn">图未生成</span>
                      <span v-else-if="r.noSlot" class="kf-tag warn">无可用槽位</span>
                      <span v-else class="kf-tag plan">按槽位推导</span>
                    </div>
                    <div class="kf-ref-role">
                      {{ r.roleLabel }}
                      <span v-if="r.slot" class="kf-ref-slot">{{ r.slot }}</span>
                      <span v-if="r.required" class="kf-ref-req">必需</span>
                    </div>
                    <div class="kf-ref-input">输入位置 {{ r.inputKey }}</div>
                  </div>
                </div>
              </div>
              <div v-else class="detail-hint-block">
                该镜头暂无可展示的参考图：{{ kfMaxRefs ? '提示词中没有可注入的 @角色 / @场景 / @道具引用，请先补齐对应资产' : '当前生图工作流为纯文生图，未声明参考图槽位' }}。
              </div>
            </n-spin>
            <div class="detail-source-hint">
              真实入参＝该镜头重生成任务实际记录的参考图；按槽位推导＝按提示词引用与工作流槽位规则会传入的图；无可用槽位＝该类型槽位已用尽或工作流未声明，本次不会传入；图未生成＝对应定妆图 / 基准图尚未生成，请先到资产库补齐。
            </div>
          </section>

          <!-- 分区二：分镜图提示词 -->
          <section class="detail-section">
            <div class="detail-section-head">
              <span class="detail-section-index">2</span>
              <span class="detail-section-name">分镜图提示词</span>
            </div>
            <n-input
              :value="promptDraft[detailShot.id] ?? detailShot.image_prompt ?? ''"
              type="textarea"
              :rows="5"
              placeholder="分镜图提示词"
              @update:value="(v: string) => onPromptInput(detailShot!, v)"
            />
            <div class="detail-hint">
              参考图按 image1→image3 顺序对应角色身份、角色服装、场景基准图；未注入的槽位在提交前会被剔除。@角色名 / @场景名 会在生成时自动解析为对应资产图。
            </div>
          </section>

          <!-- 分区三：生成操作 -->
          <section class="detail-section">
            <div class="detail-section-head">
              <span class="detail-section-index">3</span>
              <span class="detail-section-name">生成操作</span>
            </div>
            <div v-if="!reviewApproved" class="detail-gate">公共资产尚未通过人工审核，暂不可生成分镜图。</div>
            <div class="detail-actions">
              <n-button
                type="primary"
                :disabled="!reviewApproved || !!detailShot.current_assets?.keyframe?.locked"
                :loading="generateMutation.isPending.value"
                @click="handleGenerate(detailShot)"
              >{{ detailShot.current_assets?.keyframe ? '重新生成分镜图' : '生成分镜图' }}</n-button>
            </div>
          </section>
        </template>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.sb-images-page {
  padding: 16px;
  min-height: calc(100vh - var(--app-header-height, 56px));
}
.gate-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 12px;
  background: var(--bg-panel, #fff);
}
.gate-banner.ok {
  border-color: rgba(34, 197, 94, 0.35);
  background: rgba(34, 197, 94, 0.06);
}
.gate-banner.bad {
  border-color: rgba(239, 68, 68, 0.35);
  background: rgba(239, 68, 68, 0.06);
}
.gate-badge { font-size: 14px; }
.gate-text { font-size: 13px; color: var(--text-primary, #333); margin-right: auto; }

/* ===== 生成总体进度面板 ===== */
.gen-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 12px;
  background: var(--bg-panel, #fff);
}
.gen-panel.done { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.06); }
.gen-panel.fail { border-color: rgba(239, 68, 68, 0.35); }
.gen-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.gen-badge { font-size: 14px; }
.gen-title { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); }
.gen-nums { font-size: 12px; color: var(--text-secondary, #64748b); margin-left: auto; }
.gen-bar { width: 100%; }
.gen-sub { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 12px; }
.gen-state { color: var(--text-secondary, #64748b); }
.gen-state.queued { color: #b45309; }
.gen-state.running { color: var(--brand-primary, #6366f1); }
.gen-state.ok { color: #15803d; font-weight: 500; }
.gen-state.fail { color: #b91c1c; display: inline-flex; align-items: center; gap: 8px; }
.gen-inflight { display: flex; gap: 6px; flex-wrap: wrap; }
.gen-inflight-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--bg-elevated, #f8fafc);
  border: 1px solid var(--border-subtle, #e5e7eb);
}
.gen-inflight-name { color: var(--text-primary, #333); }
.gen-inflight-state { color: var(--text-secondary, #64748b); }
.gen-inflight-state.running { color: var(--brand-primary, #6366f1); }
.gen-inflight-state.queued { color: #b45309; }
/* 镜头卡片上的失败原因与生成中占位 */
.shot-error {
  font-size: 11px;
  color: #b91c1c;
  line-height: 1.5;
  background: rgba(239, 68, 68, 0.08);
  border-radius: 6px;
  padding: 4px 8px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.thumb-generating { color: var(--brand-primary, #6366f1); font-size: 12px; }
.shot-card.is-generating { border-color: rgba(99, 102, 241, 0.45); background: rgba(99, 102, 241, 0.04); }
.shot-card.is-failed { border-color: rgba(239, 68, 68, 0.4); }
.summary-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.page-loading { display: block; margin: 48px auto; }
.state-block { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 48px 24px; }
.state-text { margin: 0; color: var(--text-secondary); font-size: 14px; }
.scene-group { margin-bottom: 20px; }
.scene-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.scene-seq { color: var(--text-tertiary); font-size: 13px; }
.scene-title { font-weight: 600; font-size: 14px; }
.shots-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(188px, 1fr)); gap: 12px; }
.shot-card { display: flex; flex-direction: column; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: var(--radius, 10px); overflow: hidden; background: var(--bg-panel, #fff); }
.shot-thumb { aspect-ratio: 16 / 9; max-height: 118px; background: var(--bg-elevated, #f5f5f5); display: flex; align-items: center; justify-content: center; overflow: hidden; cursor: pointer; transition: border-color 0.15s; }
.shot-thumb:hover { filter: brightness(0.97); }
.shot-thumb-img { width: 100%; height: 100%; display: block; }
.shot-thumb :deep(img) { width: 100%; height: 100%; object-fit: cover; }
.thumb-empty { font-size: 13px; color: var(--text-tertiary); }
.shot-body { padding: 12px; display: flex; flex-direction: column; gap: 8px; flex: 1; }
.shot-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.shot-name { font-size: 14px; font-weight: 600; }
.shot-actions { margin-top: auto; flex-wrap: wrap; }
.shot-body :deep(.n-input__textarea-el) { font-size: 13px; }
.hidden-file { display: none; }
.bar-gap { flex: 1; }
.shot-thumb { position: relative; }
/* 覆盖层只承载勾选与状态标记，不拦截图片点击，否则缩略图的大图预览点不出来 */
.thumb-overlay { position: absolute; top: 6px; left: 6px; right: 6px; display: flex; align-items: center; justify-content: space-between; z-index: 2; pointer-events: none; }
.thumb-overlay > * { pointer-events: auto; }
.select-box { display: inline-flex; align-items: center; justify-content: center; padding: 2px; border-radius: 6px; background: rgba(255, 255, 255, 0.72); backdrop-filter: blur(2px); cursor: pointer; transition: background 0.15s; }
.select-box:hover { background: rgba(255, 255, 255, 0.92); }
.select-box :deep(.select-box-cb) { pointer-events: none; }
.select-box.checked { background: rgba(255, 255, 255, 0.92); }
/* ===== 镜头详情（分镜图） ===== */
.detail-hero { margin-bottom: 4px; }
.detail-img { width: 100%; border-radius: 8px; background: #000; display: block; }
.detail-img :deep(img) { width: 100%; max-height: 300px; object-fit: contain; display: block; }
.detail-empty {
  padding: 28px 16px;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  border-radius: 10px;
  text-align: center;
  font-size: 13px;
  color: var(--text-secondary, #666);
  background: var(--bg-elevated, #fafafa);
}
.detail-meta { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
.detail-name { font-size: 14px; font-weight: 600; }
.detail-status { font-size: 12px; color: var(--text-tertiary, #909399); }
.detail-section { padding: 14px 0; border-bottom: 1px solid var(--divider-color, #efeff5); }
.detail-section:last-of-type { border-bottom: none; padding-bottom: 4px; }
.detail-section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.detail-section-index {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--brand-primary, #6366f1);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  flex-shrink: 0;
}
.detail-section-name { font-size: 14px; font-weight: 600; }
.detail-section-hint { margin-left: auto; font-size: 11px; color: var(--text-tertiary, #909399); }
.kf-ref-list { display: flex; flex-direction: column; gap: 10px; }
.kf-ref-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px;
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 8px;
  background: var(--bg-panel, #fff);
}
.kf-ref-meta { flex: 1; min-width: 0; }
.kf-ref-name {
  font-size: 13px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kf-ref-role { margin-top: 3px; font-size: 11px; color: var(--text-tertiary, #909399); }
.kf-ref-slot {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-elevated, #f5f7fa);
  font-family: monospace;
  font-size: 10px;
}
.kf-ref-req {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(245, 158, 11, 0.12);
  color: #b45309;
}
.kf-ref-input { margin-top: 2px; font-size: 10px; color: var(--text-tertiary, #909399); font-family: monospace; }
.kf-ref-thumb { flex-shrink: 0; }
.kf-ref-empty {
  width: 52px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  border-radius: 6px;
  font-size: 11px;
  color: var(--text-tertiary, #909399);
}
.kf-tag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; font-size: 10px; font-weight: 400; }
.kf-tag.ok { background: rgba(34, 197, 94, 0.12); color: #15803d; }
.kf-tag.plan { background: rgba(99, 102, 241, 0.12); color: #4338ca; }
.kf-tag.warn { background: rgba(245, 158, 11, 0.14); color: #b45309; }
.detail-source-hint { margin-top: 8px; font-size: 11px; line-height: 1.6; color: var(--text-tertiary, #909399); }
.detail-hint { margin-top: 6px; font-size: 11px; line-height: 1.6; color: var(--text-tertiary, #909399); }
.detail-hint-block {
  padding: 12px;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary, #909399);
}
.detail-gate {
  margin-bottom: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.1);
  font-size: 12px;
  color: #b45309;
}
.detail-actions { display: flex; gap: 8px; }
@media (max-width: 768px) {
  .gate-banner { flex-wrap: wrap; }
  .scene-header { flex-wrap: wrap; }
  .shot-actions { flex-wrap: wrap; }
  .shots-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .shot-thumb { max-height: 100px; }
}
@media (max-width: 768px) {
  .detail-section { padding: 12px 0; }
  .detail-section-name { font-size: 13px; }
  .kf-ref-item { gap: 10px; padding: 8px 6px; }
  .kf-ref-name { white-space: normal; }
  .detail-actions :deep(.n-button) { flex: 1; }
}
@media (max-width: 480px) {
  .shots-grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
  .shot-thumb { max-height: 96px; }
  .detail-section { padding: 10px 0; }
  .detail-hint, .detail-source-hint { font-size: 11px; }
  .kf-ref-input, .kf-ref-slot { font-size: 10px; }
  .detail-actions :deep(.n-button) { width: 100%; }
}
</style>
