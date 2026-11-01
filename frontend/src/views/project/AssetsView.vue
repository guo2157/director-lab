<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpace, NSpin, NEmpty, NTag, NDrawer, NDrawerContent,
  NInput, NTooltip, NModal, NAlert, NCheckbox, NProgress, useMessage, useDialog,
  NImage, NDropdown,
} from 'naive-ui'
import {
  listAssets, deleteAsset, lockAsset, unlockAsset,
  replaceAsset, regenerateAsset, getAssetRegenerateConfig,
} from '@/api/assets'
import type { AssetRegenerateConfig } from '@/api/assets'
import type { Asset } from '@/types/asset'
import { fetchCandidates, adoptCandidate } from '@/api/regeneration'
import type { CandidateAsset } from '@/api/regeneration'
import { getJob } from '@/api/jobs'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { getProject, reviewProjectAssets } from '@/api/projects'
import { generateCharacterPortrait, generatePropAnchor, generateSceneBaseline } from '@/api/storyboard'
import { getAssetGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { AssetGenerationJob, AssetGenerationProgress } from '@/types/generation'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- 人工审核闸口（资产库 → 分镜图 的必经环节） ---
const projectQuery = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
})
const assetsReviewStatus = computed(() => projectQuery.data.value?.assets_review_status ?? 'pending')
const reviewStatusText = computed(() => {
  if (assetsReviewStatus.value === 'approved') return '公共资产已通过人工审核，可进入分镜图生成'
  if (assetsReviewStatus.value === 'rejected') return '审核被驳回：请修改或替换不满意的资产后重新提交审核'
  return '公共资产待人工审核：确认资产无误并通过审核后，方可生成分镜图'
})
const reviewMutation = useMutation({
  mutationFn: (action: 'approve' | 'reject') => reviewProjectAssets(projectId.value, action),
  onSuccess: (data) => {
    message.success(
      data.assets_review_status === 'approved' ? '审核通过，已放行分镜图生成' : '已驳回，请修改或替换资产后重新提交',
    )
    queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e: any) => message.error(`审核失败: ${e?.message ?? e}`),
})

// --- Category tree ---
// 分类值必须与后端权威枚举 enums.ASSET_TYPE 完全一致，否则后端过滤条件永远无匹配
// 资产库只负责项目级公共资产；分镜图/分镜视频属于集级资产，已独立到分镜图与分镜看板页面
// 一级分类：全部（角色+场景+道具合集）/ 角色 / 场景 / 道具（资产库只负责基础资产管理）
const categories = [
  { label: '全部', value: '' },
  { label: '角色', value: 'character' },
  { label: '场景', value: 'scene' },
  { label: '道具', value: 'prop' },
]
const activeCategory = ref('')
const keyword = ref('')
const ALL_PAGE_SIZE = 100

// --- Data fetching ---
function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败，请稍后重试'
}
/** 全量拉取：按 100 条/页循环翻页直到取满 total，资产库一次性展示全部内容 */
async function listAllAssets() {
  const items: Asset[] = []
  let current = 1
  let total = 0
  for (;;) {
    const res = await listAssets({
      project_id: projectId.value,
      asset_type: activeCategory.value || undefined,
      keyword: keyword.value || undefined,
      page: current,
      page_size: ALL_PAGE_SIZE,
    })
    const batch = res?.items ?? []
    total = res?.total ?? items.length + batch.length
    items.push(...batch)
    if (!batch.length || items.length >= total) break
    current += 1
  }
  return { items, total }
}

const assetsQuery = useQuery({
  queryKey: ['assets', projectId, activeCategory, keyword],
  queryFn: () => listAllAssets(),
})

// --- Upload（独立上传入口已移除）：单独上传的角色/场景/道具资产无法绑定实体参考关系，
// 资产图统一走角色/场景/道具卡片的生成入口，替换走资产卡片的『上传替换』 ---

// --- Lock/Unlock ---
const lockMutation = useMutation({
  mutationFn: (params: { id: string; lock: boolean }) =>
    params.lock ? lockAsset(params.id) : unlockAsset(params.id),
  onSuccess: () => {
    message.success('操作成功')
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] })
  },
  onError: (e: any) => message.error(`操作失败: ${e?.message ?? e}`),
})

// --- Delete ---
const deleteMutation = useMutation({
  mutationFn: (id: string) => deleteAsset(id),
  onSuccess: () => {
    message.success('删除成功')
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] })
  },
  onError: (e: any) => message.error(`删除失败: ${e?.message ?? e}`),
})

// --- Drawer ---
const drawerVisible = ref(false)
const activeAsset = ref<Asset | null>(null)
const drawerPrompt = ref('')
const drawerPromptVersion = ref('')

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制到剪贴板')
  } catch {
    message.error('复制失败，请手动选择复制')
  }
}

async function openDrawer(asset: Asset) {
  activeAsset.value = asset
  drawerVisible.value = true
  drawerPrompt.value = ''
  drawerPromptVersion.value = ''
  // 展示用：可重新生成的资产附带提示词（只读展示，读取失败不影响详情）
  if (canRegenerate(asset)) {
    try {
      const cfg = await getAssetRegenerateConfig(asset.id)
      drawerPrompt.value = cfg.prompt || ''
      drawerPromptVersion.value = cfg.asset_prompt_version || ''
    } catch {
      drawerPrompt.value = ''
    }
  }
}

/** 替换或重新生成后，同时刷新资产库与实体齐全度 */
async function refreshAll() {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ['assets', projectId] }),
    queryClient.invalidateQueries({ queryKey: ['characters', projectId] }),
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] }),
    queryClient.invalidateQueries({ queryKey: ['props', projectId] }),
  ])
}

// --- 上传替换 ---
const replaceVisible = ref(false)
const replaceTarget = ref<Asset | null>(null)
const replaceFileObj = ref<File | null>(null)
const replacePreview = ref<string | null>(null)
const replaceBusy = ref(false)

/** 资产图片 URL 追加版本参数：替换/更新后 updated_at 变化，URL 随之变化，避免浏览器缓存旧图 */
function assetImgUrl(asset: Asset): string {
  const base = asset.thumb_url || asset.url || ''
  if (!base) return ''
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}v=${encodeURIComponent(asset.updated_at)}`
}

function openReplace(asset: Asset) {
  replaceTarget.value = asset
  replaceFileObj.value = null
  if (replacePreview.value) {
    URL.revokeObjectURL(replacePreview.value)
    replacePreview.value = null
  }
  replaceVisible.value = true
}

function onReplaceFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  replaceFileObj.value = file
  if (replacePreview.value) URL.revokeObjectURL(replacePreview.value)
  replacePreview.value = file.type.startsWith('image/') ? URL.createObjectURL(file) : null
  input.value = ''
}
/** 替换弹窗文件选择框引用：程序化触发（label 包裹 n-button 不会转发点击，与页面上方上传按钮同款修复） */
const replaceFileInputRef = ref<HTMLInputElement | null>(null)
function triggerReplaceFileSelect() {
  replaceFileInputRef.value?.click()
}

async function confirmReplace() {
  if (!replaceTarget.value || !replaceFileObj.value) return
  replaceBusy.value = true
  try {
    const res = await replaceAsset(replaceTarget.value.id, replaceFileObj.value)
    message.success('替换成功，图片内容已更新（名称与位置保持不变）')
    if (res.affected_shots?.length) {
      message.info(`${res.affected_shots.length} 个引用镜头已标记待重做`)
    }
    replaceVisible.value = false
    // 立即用替换结果更新本地缓存：updated_at 变化使图片 URL 的 v 参数变化，无需等待重新拉取即显示新图
    const cacheKey = ['assets', projectId]
    const cached = queryClient.getQueryData<{ items: Asset[] }>(cacheKey)
    if (cached?.items) {
      const idx = cached.items.findIndex((a) => a.id === res.id)
      if (idx >= 0) {
        const nextItems = [...cached.items]
        nextItems[idx] = { ...nextItems[idx], ...res }
        queryClient.setQueryData(cacheKey, { ...cached, items: nextItems })
      }
    }
    if (activeAsset.value?.id === res.id) {
      activeAsset.value = { ...activeAsset.value, ...res }
    }
    await refreshAll()
  } catch (e: any) {
    message.error(`替换失败: ${e?.message ?? e}`)
  } finally {
    replaceBusy.value = false
  }
}

// --- 重新生成（角色/场景/道具：可改提示词，结果落候选版本） ---
const REGEN_TYPES = ['character', 'scene', 'prop']
function canRegenerate(asset: Asset): boolean {
  return REGEN_TYPES.includes(asset.asset_type)
}

// --- 卡片更多操作下拉（锁定/删除收进菜单，删除保留二次确认） ---
function cardMenuOptions(asset: Asset) {
  return [
    { label: asset.locked ? '解锁' : '锁定', key: 'lock' },
    {
      label: '删除',
      key: 'delete',
      disabled: asset.locked,
      props: { style: 'color: var(--danger, #ef4444);' },
    },
  ]
}
function onCardMenu(key: string | number, asset: Asset) {
  if (key === 'lock') {
    lockMutation.mutate({ id: asset.id, lock: !asset.locked })
  } else if (key === 'delete') {
    dialog.warning({
      title: '删除资产',
      content: `确认删除资产「${asset.name || asset.id}」？删除后不可恢复。`,
      positiveText: '删除',
      negativeText: '取消',
      onPositiveClick: () => deleteMutation.mutate(asset.id),
    })
  }
}

const regenVisible = ref(false)
const regenAsset = ref<Asset | null>(null)
const regenLoading = ref(false)
const regenPrompt = ref('')
const regenNegative = ref('')
const regenPromptVersion = ref('')
const regenSubmitting = ref(false)
const regenJobStatus = ref('')
const regenErrorMsg = ref('')
const regenCandidates = ref<CandidateAsset[]>([])
const adoptingId = ref<string | null>(null)
let regenTimer: number | null = null

function stopRegenPoll() {
  if (regenTimer !== null) {
    window.clearInterval(regenTimer)
    regenTimer = null
  }
}

async function loadCandidates(assetId: string) {
  try {
    const res = await fetchCandidates(assetId)
    regenCandidates.value = res?.candidates ?? []
  } catch {
    regenCandidates.value = []
  }
}

function startRegenPoll(jobId: string) {
  stopRegenPoll()
  regenTimer = window.setInterval(async () => {
    try {
      const job = await getJob(jobId)
      regenJobStatus.value = job.status
      if (['succeeded', 'failed', 'lost', 'cancelled'].includes(job.status)) {
        stopRegenPoll()
        if (job.status === 'succeeded') {
          message.success('重新生成完成，请在下方候选版本中确认采用')
          if (regenAsset.value) {
            await loadCandidates(regenAsset.value.id)
            await refreshAll()
          }
        } else {
          regenErrorMsg.value = (job as any).error_msg || `任务${job.status}`
        }
      }
    } catch {
      stopRegenPoll()
    }
  }, 3000)
}

async function openRegenerate(asset: Asset) {
  regenAsset.value = asset
  regenVisible.value = true
  regenLoading.value = true
  regenErrorMsg.value = ''
  regenJobStatus.value = ''
  regenCandidates.value = []
  stopRegenPoll()
  try {
    const cfg: AssetRegenerateConfig = await getAssetRegenerateConfig(asset.id)
    regenPrompt.value = cfg.prompt
    regenNegative.value = cfg.negative_prompt
    regenPromptVersion.value = cfg.asset_prompt_version
    await loadCandidates(asset.id)
  } catch (e: any) {
    regenErrorMsg.value = e?.message ?? '读取重新生成配置失败'
  } finally {
    regenLoading.value = false
  }
}

async function submitRegenerate() {
  if (!regenAsset.value) return
  regenSubmitting.value = true
  regenErrorMsg.value = ''
  try {
    const res = await regenerateAsset(regenAsset.value.id, {
      prompt: regenPrompt.value,
      negative_prompt: regenNegative.value,
      idempotency_key: `asset_regen_${regenAsset.value.id}_${Date.now()}`,
    })
    regenJobStatus.value = res.status
    message.success('重新生成任务已提交，正在生成（进度见页面顶部进度面板）')
    startRegenPoll(res.job_id)
    // 重新生成同样属于资产图生产：让顶部进度面板也跟踪它，用户不打开抽屉也能看到进度
    await assetProgress.start({ notifyOnComplete: true })
  } catch (e: any) {
    regenErrorMsg.value = e?.message ?? '提交失败'
  } finally {
    regenSubmitting.value = false
  }
}

async function adoptRegenCandidate(candidateId: string) {
  adoptingId.value = candidateId
  try {
    await adoptCandidate(candidateId)
    message.success('已采用该候选版本')
    await refreshAll()
    if (regenAsset.value) await loadCandidates(regenAsset.value.id)
  } catch (e: any) {
    message.error(`采用失败: ${e?.message ?? e}`)
  } finally {
    adoptingId.value = null
  }
}

const assetTypeLabels: Record<string, string> = {
  character: '角色', scene: '场景', prop: '道具',
  keyframe: '分镜图', clip: '视频', audio: '音频', last_frame: '末帧',
  reference: '参考图', first_frame: '首帧', end_frame: '末帧',
}

// --- 卡片状态点（生成状态角标） ---
const statusDotClass = (status?: string | null): string => {
  const s = (status ?? '').toLowerCase()
  if (['done', 'ready', 'success', 'completed', 'active'].includes(s)) return 'ok'
  if (['fail', 'failed', 'error'].includes(s)) return 'err'
  if (['pending', 'processing', 'generating', 'running', 'queued'].includes(s)) return 'busy'
  return 'idle'
}
const statusLabel = (status?: string | null): string => status || '未知状态'

// --- 统一 status-tag 样式映射（global.css：.status-tag 及 is-* 变体） ---
const ASSET_STATUS_LABELS: Record<string, string> = {
  pending: '待处理', current: '当前版本', stale: '已过期', archived: '已归档',
}
const assetStatusLabel = (status?: string | null): string =>
  ASSET_STATUS_LABELS[(status ?? '').toLowerCase()] || statusLabel(status)
const assetStatusTagClass = (status?: string | null): string => {
  const s = (status ?? '').toLowerCase()
  if (s === 'current') return 'is-succeeded'
  if (s === 'stale') return 'is-stale'
  if (s === 'archived') return 'is-locked'
  return 'is-pending'
}
const reviewStatusTagClass = (status: string): string =>
  status === 'approved' ? 'is-succeeded' : status === 'rejected' ? 'is-failed' : 'is-pending'
const reviewStatusTagLabel = (status: string): string =>
  status === 'approved' ? '审核通过' : status === 'rejected' ? '已驳回' : '待审核'

// --- 拖拽资产（供分镜槽位绑定） ---
function handleAssetDragStart(e: DragEvent, asset: Asset) {
  if (!e.dataTransfer) return
  e.dataTransfer.setData('application/x-asset-id', asset.id)
  e.dataTransfer.setData('text/plain', asset.id)
  e.dataTransfer.effectAllowed = 'copy'
}

// --- 自适应卡片网格（ResizeObserver 动态列数） ---
const gridRef = ref<HTMLElement | null>(null)
const gridWidth = ref(0)
const MIN_CARD_WIDTH = 200
const MAX_CARD_WIDTH = 320
const GRID_GAP = 12
let gridObserver: ResizeObserver | null = null
onMounted(() => {
  // 刷新页面/再次进入时也要能看到当前生成进度（有在途任务才轮询）
  void assetProgress.bootstrap()
  if (!gridRef.value) return
  gridObserver = new ResizeObserver((entries) => {
    for (const entry of entries) gridWidth.value = entry.contentRect.width
  })
  gridObserver.observe(gridRef.value)
})
onBeforeUnmount(() => {
  gridObserver?.disconnect()
  gridObserver = null
  stopRegenPoll()
  if (replacePreview.value) URL.revokeObjectURL(replacePreview.value)
})
const assetGridStyle = computed(() => {
  if (!gridWidth.value) return undefined
  const gap = GRID_GAP
  const maxColsByMin = Math.max(1, Math.floor((gridWidth.value + gap) / (MIN_CARD_WIDTH + gap)))
  const idealCols = Math.max(1, Math.round((gridWidth.value + gap) / (MAX_CARD_WIDTH + gap)))
  const cols = Math.min(Math.max(idealCols, 1), maxColsByMin)
  return { gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap: `${gap}px` }
})

/** 资产分组展示：「全部」时按角色/场景/道具分组一次展示，具体分类时单组展示，不使用下拉切换 */
const assetGroups = computed(() => {
  const items = assetsQuery.data.value?.items || []
  const defs = [
    { type: 'character', label: '角色' },
    { type: 'scene', label: '场景' },
    { type: 'prop', label: '道具' },
  ]
  const groups = defs.map((d) => ({ ...d, items: items.filter((a) => a.asset_type === d.type) }))
  // 具体分类只展示该分类分组，避免其它分类以 0 计数形式出现在页面上
  if (!activeCategory.value) return groups
  return groups.filter((g) => g.type === activeCategory.value)
})

// --- 资产齐全度检查（角色/场景/道具参考图生成进度） ---
/** 通用全量拉取：循环翻页直到取满 total（角色/场景/道具均展示全部条目） */
async function fetchAllPages<T>(
  fetcher: (params: { page: number; page_size: number }) => Promise<{ items?: T[]; total?: number }>,
): Promise<{ items: T[]; total: number }> {
  const items: T[] = []
  let current = 1
  for (;;) {
    const res = await fetcher({ page: current, page_size: ALL_PAGE_SIZE })
    const batch = res?.items ?? []
    const total = res?.total ?? items.length + batch.length
    items.push(...batch)
    if (!batch.length || items.length >= total) return { items, total }
    current += 1
  }
}

const { data: charsData } = useQuery({
  queryKey: ['characters', projectId],
  queryFn: () => fetchAllPages((p) => listCharacters(projectId.value, p)),
  enabled: computed(() => !!projectId.value),
})
const { data: scenesData } = useQuery({
  queryKey: ['scenes', projectId],
  queryFn: () => fetchAllPages((p) => listScenes(projectId.value, { ...p, include_shots: false })),
  enabled: computed(() => !!projectId.value),
})
const { data: propsData } = useQuery({
  queryKey: ['props', projectId],
  queryFn: () => fetchAllPages((p) => listProps(projectId.value, p)),
  enabled: computed(() => !!projectId.value),
})
const readiness = computed(() => {
  const cs = charsData.value?.items ?? []
  const ss = (scenesData.value?.items ?? []) as Array<{ baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ anchor_asset_id?: string | null }>
  // 与页面展示口径保持一致：制作资产区列出全部角色/场景/道具，齐全度亦按全部统计
  // （道具不再按 anchor_required 过滤，否则页面上「已生成 8/8」与横幅「3/3」自相矛盾）
  const total = cs.length + ss.length + ps.length
  const done =
    cs.filter((c) => !!c.portrait_asset_id).length +
    ss.filter((s) => !!s.baseline_asset_id).length +
    ps.filter((p) => !!p.anchor_asset_id).length
  return { total, done, all: total > 0 && done === total }
})

// --- 生成进度（后端聚合：正在生成什么 / 批量进度 / 是否全部完成） ---
/** 本次会话提交的资产生成任务：用于「刚才这批」的进度条与失败重试 */
interface SubmittedAsset {
  id: string
  name: string
  kind: 'character' | 'scene' | 'prop'
  unit: string
}
const submittedAssets = ref<SubmittedAsset[]>([])
const submittedJobIds = ref<string[]>([])

const assetProgress = useGenerationProgress<AssetGenerationProgress>({
  fetcher: () => getAssetGenerationProgress(projectId.value),
  onChange: async () => {
    // 出图完成后重新拉取角色/场景/道具与资产列表，让卡片与图片立即更新
    await refreshAll()
  },
  onAllComplete: () => {
    message.success('公共资产图已全部生成完成，请检查效果后提交人工审核')
  },
})

/** 资产图分组统计（角色/场景/道具）：模板里安全遍历，避免 Partial 记录取值可能为空 */
const progressGroups = computed(() => {
  const groups = assetProgress.data.value?.groups ?? {}
  const defs: Array<{ kind: 'character' | 'scene' | 'prop'; label: string }> = [
    { kind: 'character', label: '角色定妆图' },
    { kind: 'scene', label: '场景基准图' },
    { kind: 'prop', label: '道具锚定图' },
  ]
  return defs
    .map((d) => ({ ...d, ...(groups[d.kind] ?? { total: 0, done: 0 }) }))
    .filter((g) => g.total > 0)
})

/** 在途任务按角色/场景/道具分组：直接回答「正在生成的是什么类型的图」 */
const inflightGroups = computed(() => {
  const defs = [
    { kind: 'character', label: '角色', unit: '定妆图' },
    { kind: 'scene', label: '场景', unit: '基准图' },
    { kind: 'prop', label: '道具', unit: '锚定图' },
  ]
  const list = assetProgress.data.value?.inflight ?? []
  return defs
    .map((d) => ({ ...d, items: list.filter((j) => j.kind === d.kind) }))
    .filter((g) => g.items.length > 0)
})

/** 在途任务 → 卡片/分组可直接查询的状态 */
const inflightById = computed(() => {
  const map = new Map<string, AssetGenerationJob>()
  for (const job of assetProgress.data.value?.inflight ?? []) map.set(job.entity_id, job)
  return map
})

/** 本次提交批次里，仍在途的任务 ID 集（与后端在途明细求交） */
const submittedInflightIds = computed(() => {
  const inflight = new Set((assetProgress.data.value?.inflight ?? []).map((j) => j.entity_id))
  return submittedAssets.value.filter((s) => inflight.has(s.id)).map((s) => s.id)
})

/** 批量进度：本次提交 N 个，其中完成 M 个 / 失败 K 个 / 仍在生成中 */
const batchProgress = computed(() => {
  const total = submittedAssets.value.length
  if (!total) return null
  const doneSet = new Set<string>()
  // 已出图判定：任一资产图字段已回写即视为完成（与齐全度同口径）
  const byId = new Map<string, boolean>()
  for (const c of (charsData.value?.items ?? []) as Array<{ id: string; portrait_asset_id?: string | null }>) {
    byId.set(c.id, !!c.portrait_asset_id)
  }
  for (const s of (scenesData.value?.items ?? []) as Array<{ id: string; baseline_asset_id?: string | null }>) {
    byId.set(s.id, !!s.baseline_asset_id)
  }
  for (const p of (propsData.value?.items ?? []) as Array<{ id: string; anchor_asset_id?: string | null }>) {
    byId.set(p.id, !!p.anchor_asset_id)
  }
  for (const s of submittedAssets.value) if (byId.get(s.id)) doneSet.add(s.id)
  const done = doneSet.size
  const failed = assetProgress.data.value?.failed_recent ?? []
  const failedIds = new Set(failed.filter((f) => submittedAssets.value.some((s) => s.id === f.entity_id)).map((f) => f.entity_id))
  const running = submittedInflightIds.value.length
  // 「已完成 + 生成中 + 失败」= total 时批次才算收尾（失败项不阻塞完成提示，但会单独提示）
  const percent = total ? Math.round((done / total) * 100) : 0
  return {
    total,
    done,
    running,
    failedIds,
    failed: [...failedIds].length,
    percent,
    finished: running === 0 && done + failedIds.size >= total,
  }
})

/** 卡片上的生成状态文案：排队 / 生成中 42% */
function inflightText(job?: AssetGenerationJob): string {
  if (!job) return ''
  if (job.status === 'running') return job.progress != null ? `生成中 ${job.progress}%` : '生成中'
  if (job.status === 'queued') return '排队中'
  return '提交确认中'
}
function formatTime(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}
/** 顶部生成状态：优先展示「正在生成什么」，其次展示「上次全部完成」 */
const assetGenHeadline = computed(() => {
  const p = assetProgress.data.value
  const inflightCount = p?.inflight_count ?? 0
  if (inflightCount > 0) {
    const parts = inflightGroups.value.map(
      (g) => `${g.label}${g.items.length}个（${g.unit}）`,
    )
    return `正在生成：${parts.join('、')}`
  }
  if (projectReadiness.value.all) {
    const at = formatTime(p?.last_completed_at)
    return `公共资产图已全部生成完成${at ? `（最近完成于 ${at}）` : ''}`
  }
  if (!projectReadiness.value.total) return '解析完成后此处展示待生成的资产'
  return `待生成：还有 ${projectReadiness.value.total - projectReadiness.value.done} 个资产图未生成`
})
/** 项目级齐全度（后端权威口径，不受左侧分类筛选影响）：进度面板与审核闸口都用它 */
const projectReadiness = computed(() => {
  const p = assetProgress.data.value
  if (p) return { total: p.total, done: p.done, all: p.all_done }
  return readiness.value
})

/** 生成失败项（近 1 小时，按实体去重） */
const failedEntities = computed(() => {
  const list = assetProgress.data.value?.failed_recent ?? []
  const seen = new Set<string>()
  return list.filter((f) => {
    if (seen.has(f.entity_id)) return false
    seen.add(f.entity_id)
    return true
  })
})
/** 失败实体 ID 集：批量 chip 与重试按钮共用 */
const failedEntityIds = computed(() => new Set(failedEntities.value.map((f) => f.entity_id)))
// --- 制作资产区（公共资产生成：角色定妆图/场景基准图/道具锚定图） ---
const craftingId = ref<string | null>(null)
const batchCrafting = ref(false)
// --- 制作资产勾选批量生成：勾选多个/全选后批量触发 ---
const selectedCraftIds = ref<string[]>([])
function toggleCraft(id: string, checked: boolean) {
  const set = new Set(selectedCraftIds.value)
  if (checked) set.add(id)
  else set.delete(id)
  selectedCraftIds.value = [...set]
}
function isGroupAllSelected(grp: { items: { id: string }[] }) {
  return grp.items.length > 0 && grp.items.every((i) => selectedCraftIds.value.includes(i.id))
}
function toggleGroupAll(grp: { items: { id: string }[] }, checked: boolean) {
  const set = new Set(selectedCraftIds.value)
  for (const it of grp.items) {
    if (checked) set.add(it.id)
    else set.delete(it.id)
  }
  selectedCraftIds.value = [...set]
}
const genPortrait = useMutation({
  mutationFn: (id: string) => generateCharacterPortrait(id),
  onSuccess: () => {
    message.success('角色定妆图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['characters', projectId] })
    // 公共资产变更会使审核结论失效（后端回退为 pending），同步刷新审核状态
    queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
const genBaseline = useMutation({
  mutationFn: (id: string) => generateSceneBaseline(id),
  onSuccess: () => {
    message.success('场景基准图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
    // 公共资产变更会使审核结论失效（后端回退为 pending），同步刷新审核状态
    queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
const genAnchor = useMutation({
  mutationFn: (id: string) => generatePropAnchor(id),
  onSuccess: () => {
    message.success('道具锚定图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['props', projectId] })
    // 公共资产变更会使审核结论失效（后端回退为 pending），同步刷新审核状态
    queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
interface CraftItem {
  id: string
  name: string
  prompt: string
  done: boolean
  kind: 'character' | 'scene' | 'prop'
}
const craftGroups = computed(() => {
  const cs = (charsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; portrait_asset_id?: string | null }>
  const ss = (scenesData.value?.items ?? []) as Array<{ id: string; title?: string; description?: string; baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; anchor_asset_id?: string | null }>
  return [
    {
      type: 'character', label: '角色', unit: '定妆图',
      items: cs.map((c) => ({ id: c.id, name: c.name, prompt: c.description ?? '', done: !!c.portrait_asset_id, kind: 'character' as const })) as CraftItem[],
    },
    {
      type: 'scene', label: '场景', unit: '基准图',
      items: ss.map((s) => ({ id: s.id, name: s.title ?? s.id, prompt: s.description ?? '', done: !!s.baseline_asset_id, kind: 'scene' as const })) as CraftItem[],
    },
    {
      type: 'prop', label: '道具', unit: '锚定图',
      items: ps.map((p) => ({ id: p.id, name: p.name, prompt: p.description ?? '', done: !!p.anchor_asset_id, kind: 'prop' as const })) as CraftItem[],
    },
  ]
})
/** 当前分类下可见的制作资产分组：「全部」时展示角色/场景/道具全部分组，否则仅展示当前分类的条目 */
const visibleCraftGroups = computed(() => {
  if (!activeCategory.value) return craftGroups.value
  return craftGroups.value.filter((g) => g.type === activeCategory.value)
})
/** 制作资产区副标题：随当前分类收敛，仅描述当前分类需要生成的图片类型 */
const craftSubText = computed(() => {
  const unitMap: Record<string, string> = {
    character: '角色定妆图',
    scene: '场景基准图',
    prop: '道具锚定图',
  }
  const head = activeCategory.value
    ? (unitMap[activeCategory.value] ?? '')
    : '角色定妆图 / 场景基准图 / 道具锚定图'
  return `${head}（项目级公共资产，按名称绑定供分镜引用）`
})
/** 提交批次：记录本次提交的资产与任务 ID，用于批次进度条与失败重试 */
function trackSubmitted(items: CraftItem[], jobIds: string[]) {
  const unitMap: Record<string, string> = { character: '定妆图', scene: '基准图', prop: '锚定图' }
  submittedAssets.value = items.map((it) => ({
    id: it.id,
    name: it.name,
    kind: it.kind,
    unit: unitMap[it.kind] ?? '',
  }))
  submittedJobIds.value = jobIds
}

async function handleCraft(it: CraftItem) {
  craftingId.value = it.id
  try {
    if (it.kind === 'character') await genPortrait.mutateAsync(it.id)
    else if (it.kind === 'scene') await genBaseline.mutateAsync(it.id)
    else await genAnchor.mutateAsync(it.id)
    trackSubmitted([it], [])
    await assetProgress.start({ notifyOnComplete: true })
  } catch {
    // 错误提示已由 mutation onError 统一处理
  } finally {
    craftingId.value = null
  }
}

async function handleBatchCraft() {
  // 仅对勾选的制作资产批量触发：名称、占位路径、提示词均以库中定义为准
  const picked = visibleCraftGroups.value
    .flatMap((g) => g.items)
    .filter((i) => selectedCraftIds.value.includes(i.id))
  if (!picked.length) {
    message.warning('请先勾选要生成的资产')
    return
  }
  batchCrafting.value = true
  const submittedItems: CraftItem[] = []
  const jobIds: string[] = []
  const failed: string[] = []
  try {
    for (const it of picked) {
      try {
        const res =
          it.kind === 'character'
            ? await genPortrait.mutateAsync(it.id)
            : it.kind === 'scene'
              ? await genBaseline.mutateAsync(it.id)
              : await genAnchor.mutateAsync(it.id)
        submittedItems.push(it)
        const jobId = (res as { job?: { id?: string } } | undefined)?.job?.id
        if (jobId) jobIds.push(jobId)
      } catch {
        failed.push(it.name)
      }
    }
    if (submittedItems.length) {
      trackSubmitted(submittedItems, jobIds)
      message.success(`已提交 ${submittedItems.length} 个图生成任务，正在生成：见上方批次进度`)
      // 提交后立即建基线并开始轮询，全部结束后统一提示
      await assetProgress.start({ notifyOnComplete: true })
    }
    if (failed.length) message.error(`${failed.length} 个资产提交失败：${failed.slice(0, 3).join('、')}`)
    selectedCraftIds.value = []
  } finally {
    batchCrafting.value = false
  }
}

/** 失败项重试：只重提最近失败且属于本批次的资产 */
const retryingFailed = ref(false)
async function retryFailedCraft() {
  const failedIds = new Set(
    (assetProgress.data.value?.failed_recent ?? []).map((f) => f.entity_id),
  )
  const targets = submittedAssets.value.filter((s) => failedIds.has(s.id))
  if (!targets.length) return
  const all = visibleCraftGroups.value.flatMap((g) => g.items)
  const picked = targets
    .map((t) => all.find((i) => i.id === t.id && i.kind === t.kind))
    .filter((i): i is CraftItem => Boolean(i))
  retryingFailed.value = true
  try {
    const jobIds: string[] = []
    const okItems: CraftItem[] = []
    for (const it of picked) {
      try {
        const res =
          it.kind === 'character'
            ? await genPortrait.mutateAsync(it.id)
            : it.kind === 'scene'
              ? await genBaseline.mutateAsync(it.id)
              : await genAnchor.mutateAsync(it.id)
        okItems.push(it)
        const jobId = (res as { job?: { id?: string } } | undefined)?.job?.id
        if (jobId) jobIds.push(jobId)
      } catch {
        // 单个失败不阻断其余重试
      }
    }
    if (okItems.length) {
      submittedAssets.value = [
        ...submittedAssets.value.filter((s) => !okItems.some((i) => i.id === s.id)),
        ...targets.filter((t) => okItems.some((i) => i.id === t.id)),
      ]
      submittedJobIds.value = [...submittedJobIds.value, ...jobIds]
      message.success(`已重新提交 ${okItems.length} 个失败任务`)
      await assetProgress.start({ notifyOnComplete: true })
    }
  } finally {
    retryingFailed.value = false
  }
}

/** 取消本次批次的展示（不取消后端任务） */
function clearBatch() {
  submittedAssets.value = []
  submittedJobIds.value = []
}
</script>

<template>
  <div class="assets-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">资产库</h1>
      </div>
    </header>
    <div class="assets-layout">
      <!-- Category tree -->
      <div class="category-tree">
        <div
          v-for="cat in categories"
          :key="cat.value"
          class="category-item"
          :class="{ active: activeCategory === cat.value }"
          @click="activeCategory = cat.value"
        >
          {{ cat.label }}
        </div>
      </div>

      <!-- Main content -->
      <div class="assets-main">
        <!-- 生成进度面板：说明正在生成哪类图（角色/场景/道具）、批量进度、是否全部完成 -->
        <div class="gen-panel" :class="{ done: projectReadiness.all && !assetProgress.hasInflight.value, fail: failedEntities.length > 0 }">
          <div class="gen-head">
            <span class="gen-badge">{{ assetProgress.hasInflight.value ? '⏳' : projectReadiness.all ? '✓' : '•' }}</span>
            <span class="gen-title">{{ assetGenHeadline }}</span>
            <span class="gen-nums">
              {{ projectReadiness.done }}/{{ projectReadiness.total }} 已完成（{{ projectReadiness.total ? Math.round((projectReadiness.done / projectReadiness.total) * 100) : 0 }}%）
            </span>
            <n-button size="tiny" quaternary :loading="assetProgress.loading.value" @click="assetProgress.refresh()">刷新进度</n-button>
            <n-button size="small" quaternary @click="router.push(`/project/${projectId}/parse`)">返回解析页</n-button>
          </div>
          <n-progress
            type="line"
            :percentage="projectReadiness.total ? Math.round((projectReadiness.done / projectReadiness.total) * 100) : 0"
            :height="8"
            :border-radius="4"
            :show-indicator="false"
            :color="projectReadiness.all ? 'var(--state-success, #22c55e)' : 'var(--brand-primary)'"
            rail-color="var(--border-subtle, #e5e7eb)"
            class="gen-bar"
          />
          <div class="gen-groups">
            <span
              v-for="g in progressGroups"
              :key="g.kind"
              class="gen-group"
              :class="{ ok: g.total > 0 && g.done === g.total }"
            >{{ g.label }} {{ g.done }}/{{ g.total }}</span>
          </div>

          <!-- 正在生成什么：按类型分组列出条目、状态与百分比 -->
          <div v-if="inflightGroups.length" class="gen-inflight">
            <div v-for="g in inflightGroups" :key="g.kind" class="gen-inflight-group">
              <span class="gen-inflight-kind">{{ g.label }}{{ g.unit }}·{{ g.items.length }} 个</span>
              <div class="gen-inflight-items">
                <span v-for="job in g.items" :key="job.job_id" class="gen-inflight-item">
                  <span class="gen-inflight-name">{{ job.entity_name }}</span>
                  <span class="gen-inflight-state" :class="job.status">
                    {{ inflightText(job) }}
                    <template v-if="job.status === 'running' && job.progress != null">
                      <n-progress
                        type="line"
                        :percentage="job.progress"
                        :height="3"
                        :border-radius="2"
                        :show-indicator="false"
                        class="gen-mini-bar"
                      />
                    </template>
                  </span>
                </span>
              </div>
            </div>
          </div>
          <div v-else-if="!projectReadiness.all && assetProgress.data.value?.last_completed_at" class="gen-done-hint">
            最近一次生成完成于 {{ formatTime(assetProgress.data.value?.last_completed_at) }}
          </div>

          <!-- 本次提交批次进度：本次提交 N 个 / 生成中 / 已完成 / 失败 -->
          <div v-if="batchProgress" class="batch-panel">
            <div class="batch-head">
              <span class="batch-title">本批任务进度</span>
              <span class="batch-nums">
                共 {{ batchProgress.total }} 个 · 已完成 {{ batchProgress.done }} · 生成中 {{ batchProgress.running }}<template v-if="batchProgress.failed"> · 失败 {{ batchProgress.failed }}</template>
              </span>
              <n-button size="tiny" quaternary @click="clearBatch">收起</n-button>
            </div>
            <n-progress
              type="line"
              :percentage="batchProgress.percent"
              :height="6"
              :border-radius="3"
              :show-indicator="false"
              :status="batchProgress.failed && !batchProgress.running ? 'error' : 'default'"
              rail-color="var(--border-subtle, #e5e7eb)"
            />
            <div class="batch-items">
              <span
                v-for="s in submittedAssets"
                :key="s.id"
                class="batch-chip"
                :class="{
                  running: submittedInflightIds.includes(s.id),
                  fail: failedEntityIds.has(s.id),
                }"
              >{{ s.name }}</span>
            </div>
          </div>

          <!-- 失败项：明确列出失败原因，提供一键重试 -->
          <div v-if="failedEntities.length" class="gen-failed">
            <span class="gen-failed-title">最近生成失败 {{ failedEntities.length }} 个：</span>
            <span v-for="f in failedEntities" :key="f.job_id" class="gen-failed-item">
              {{ f.entity_name }}（{{ f.error_msg || '未知原因' }}）
            </span>
            <n-button v-if="batchProgress" size="tiny" type="warning" :loading="retryingFailed" @click="retryFailedCraft">重试失败项</n-button>
          </div>
        </div>
        <!-- 人工审核闸口：资产库出图完毕后必须人工确认，通过后才放行分镜图 -->
        <div class="readiness-banner" :class="{ done: assetsReviewStatus === 'approved' }">
          <span class="readiness-badge">{{ assetsReviewStatus === 'approved' ? '✓' : '⏳' }}</span>
          <span class="status-tag" :class="reviewStatusTagClass(assetsReviewStatus)">{{ reviewStatusTagLabel(assetsReviewStatus) }}</span>
          <span class="readiness-text">{{ reviewStatusText }}</span>
          <n-button
            size="small"
            type="primary"
            :disabled="!projectReadiness.all"
            :loading="reviewMutation.isPending.value"
            @click="reviewMutation.mutate('approve')"
          >审核通过</n-button>
          <n-button
            size="small"
            quaternary
            :disabled="assetsReviewStatus !== 'approved'"
            :loading="reviewMutation.isPending.value"
            @click="reviewMutation.mutate('reject')"
          >驳回返工</n-button>
          <n-button
            size="small"
            type="primary"
            :disabled="assetsReviewStatus !== 'approved'"
            @click="router.push(`/project/${projectId}/storyboard-images`)"
          >去分镜图 →</n-button>
        </div>
        <!-- 制作资产区（公共资产生成） -->
        <div class="craft-section">
          <div class="craft-head">
            <span class="craft-title">制作资产区</span>
            <span class="craft-sub">{{ craftSubText }}</span>
            <n-button
              size="medium"
              type="primary"
              :loading="batchCrafting"
              :disabled="!selectedCraftIds.length"
              @click="handleBatchCraft"
            >
              {{ assetProgress.hasInflight.value ? `生成中 ${assetProgress.inflightCount.value} 个 · 批量生成（${selectedCraftIds.length}）` : `批量生成（${selectedCraftIds.length}）` }}
            </n-button>
          </div>
          <div class="craft-body">
          <div v-for="grp in visibleCraftGroups" :key="grp.type" class="craft-group">
            <div class="craft-group-head">
              <n-checkbox
                :checked="isGroupAllSelected(grp)"
                :disabled="!grp.items.length"
                @update:checked="(v: boolean) => toggleGroupAll(grp, v)"
              >全选</n-checkbox>
              <span class="craft-group-label">{{ grp.label }}（{{ grp.items.length }}）</span>
              <span class="craft-group-done">{{ grp.items.filter((i) => i.done).length }}/{{ grp.items.length }} 已生成</span>
            </div>
            <!-- 当前分类：展示该分类需要生成的图片卡片 -->
            <div v-if="grp.items.length" class="craft-grid">
              <div
                v-for="it in grp.items"
                :key="it.id"
                class="craft-card"
                :class="{ done: it.done, inflight: !!inflightById.get(it.id), failed: failedEntityIds.has(it.id) }"
              >
                <div class="craft-card-head">
                  <n-checkbox
                    :checked="selectedCraftIds.includes(it.id)"
                    @update:checked="(v: boolean) => toggleCraft(it.id, v)"
                    @click.stop
                  />
                  <span class="craft-card-name">{{ it.name }}</span>
                  <!-- 生成中的卡片直接显示排队/百分比，不必再点开才知道进度 -->
                  <n-tag v-if="inflightById.get(it.id)" size="tiny" type="info" :bordered="false">
                    {{ inflightText(inflightById.get(it.id)) }}
                  </n-tag>
                  <n-tag v-else-if="failedEntityIds.has(it.id)" size="tiny" type="error" :bordered="false">生成失败</n-tag>
                  <n-tag v-else-if="it.done" size="tiny" type="success" :bordered="false">已生成</n-tag>
                  <n-button
                    v-else
                    size="tiny"
                    quaternary
                    type="primary"
                    :loading="craftingId === it.id"
                    :disabled="!!inflightById.get(it.id)"
                    @click="handleCraft(it)"
                  >生成{{ grp.unit }}</n-button>
                </div>
                <p class="craft-card-prompt" :title="it.prompt">{{ it.prompt || '暂无提示词描述' }}</p>
              </div>
            </div>
            <n-empty v-else size="small" :description="`暂无${grp.label}`" />
          </div>
          </div>
          <n-empty v-if="!readiness.total" size="small" description="解析完成后此处展示角色/场景/道具实体" />
        </div>
        <!-- Toolbar：独立上传入口已移除（单独上传的角色/场景/道具资产无法绑定实体参考关系，
             资产图统一走角色/场景/道具卡片的生成入口，替换走资产卡片的『上传替换』） -->

        <!-- Upload drop zone -->
        <div class="drop-zone">
          <n-spin v-if="assetsQuery.isLoading.value" class="page-loading" />
          <div v-else-if="assetsQuery.isError.value" class="state-block">
            <p class="state-text">资产加载失败：{{ errMsg(assetsQuery.error.value) }}</p>
            <n-button secondary @click="() => assetsQuery.refetch()">重试</n-button>
          </div>
          <n-empty v-else-if="assetsQuery.data.value?.items?.length === 0" description="暂无资产" />
          <!-- 按分类分组一次展示：「全部」为角色/场景/道具三组，具体分类为单组 -->
          <div v-else ref="gridRef" class="asset-groups">
            <div v-for="grp in assetGroups" :key="grp.type" class="asset-group">
              <div class="asset-group-head">
                <span class="asset-group-label">{{ grp.label }}</span>
                <span class="asset-group-count">{{ grp.items.length }}</span>
              </div>
              <div class="asset-grid" :style="assetGridStyle">
            <div
              v-for="(asset, idx) in grp.items"
              :key="asset.id"
              class="asset-card"
              :class="{ 'is-portrait': asset.asset_type === 'character' }"
              :style="{ animationDelay: `${idx * 0.05}s` }"
              draggable="true"
              @dragstart="handleAssetDragStart($event, asset)"
              @click="openDrawer(asset)"
            >
              <div class="asset-thumb">
                <img v-if="asset.thumb_url || asset.url" :src="assetImgUrl(asset)" alt="" />
                <div v-else class="thumb-placeholder">
                  <span>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</span>
                </div>
                <div v-if="asset.locked" class="lock-icon">🔒</div>
                <div class="status-dot" :class="statusDotClass(asset.status)" :title="statusLabel(asset.status)"></div>
              </div>
              <div class="asset-info">
                <div class="asset-name">{{ asset.id.split('_').slice(1).join('_') || asset.id }}</div>
                <div class="asset-meta">
                  <n-tag size="tiny" round>{{ assetTypeLabels[asset.asset_type] || asset.asset_type }}</n-tag>
                </div>
              </div>
              <div class="asset-actions" @click.stop>
                <n-tooltip>
                  <template #trigger>
                    <n-button
                      size="tiny"
                      quaternary
                      :disabled="asset.locked"
                      @click="openReplace(asset)"
                    >⬆</n-button>
                  </template>
                  {{ asset.locked ? '锁定状态不可替换' : '上传替换' }}
                </n-tooltip>
                <n-tooltip>
                  <template #trigger>
                    <n-button
                      size="tiny"
                      quaternary
                      :disabled="asset.locked || !canRegenerate(asset)"
                      @click="openRegenerate(asset)"
                    >✨</n-button>
                  </template>
                  {{ canRegenerate(asset)
                    ? (asset.locked ? '锁定状态不可重新生成' : '重新生成（可改提示词）')
                    : '分镜图/视频请在分镜看板重新生成' }}
                </n-tooltip>
                <n-dropdown
                  trigger="click"
                  :options="cardMenuOptions(asset)"
                  @select="onCardMenu($event, asset)"
                >
                  <n-button size="small" quaternary title="更多操作（锁定 / 删除）">⋯</n-button>
                </n-dropdown>
              </div>
            </div>
          </div>
            </div>
          </div>
        </div>

      </div>
    </div>

    <!-- Drawer -->
    <n-drawer v-model:show="drawerVisible" :width="'min(480px, 100vw)'">
      <n-drawer-content title="资产详情" closable>
        <template v-if="activeAsset">
          <div class="drawer-preview">
            <n-image
              v-if="activeAsset.url"
              :src="assetImgUrl(activeAsset)"
              object-fit="contain"
              class="drawer-preview-img"
            />
            <div v-else class="preview-placeholder">暂无预览</div>
          </div>
          <n-space vertical :size="16">
            <div class="drawer-title-row">
              <span class="drawer-asset-name">{{ activeAsset.name || activeAsset.id }}</span>
              <span class="status-tag" :class="assetStatusTagClass(activeAsset.status)">{{ assetStatusLabel(activeAsset.status) }}</span>
              <span v-if="activeAsset.locked" class="status-tag is-locked">已锁定</span>
            </div>
            <div class="drawer-meta">
              <div><span class="field-label">ID：</span>{{ activeAsset.id }}</div>
              <div><span class="field-label">类型：</span>{{ assetTypeLabels[activeAsset.asset_type] || activeAsset.asset_type }}</div>
              <div v-if="typeof activeAsset.reference_count === 'number'"><span class="field-label">引用次数：</span>{{ activeAsset.reference_count }}</div>
            </div>
            <div v-if="drawerPrompt" class="drawer-section">
              <div class="drawer-section-head">
                <span class="drawer-section-title">生成提示词</span>
                <span v-if="drawerPromptVersion" class="drawer-prompt-version">v{{ drawerPromptVersion }}</span>
                <n-button size="tiny" quaternary @click="copyText(drawerPrompt)">复制</n-button>
              </div>
              <div class="drawer-prompt">{{ drawerPrompt }}</div>
            </div>
            <n-space :size="8">
              <n-button size="small" :disabled="activeAsset.locked" @click="openReplace(activeAsset)">上传替换</n-button>
              <n-button size="small" type="primary" :disabled="activeAsset.locked || !canRegenerate(activeAsset)" @click="openRegenerate(activeAsset)">重新生成</n-button>
              <n-button size="small" quaternary @click="lockMutation.mutate({ id: activeAsset.id, lock: !activeAsset.locked })">{{ activeAsset.locked ? '解锁' : '锁定' }}</n-button>
            </n-space>
          </n-space>
        </template>
      </n-drawer-content>
    </n-drawer>

    <!-- 上传替换确认弹窗 -->
    <n-modal v-model:show="replaceVisible" preset="card" style="width: 640px" title="上传替换资产">
      <template v-if="replaceTarget">
        <div class="replace-card">
          <div class="replace-card-label">{{ replaceTarget.name }}</div>
          <div class="replace-card-preview">
            <input ref="replaceFileInputRef" type="file" accept="image/*,video/*" style="display:none" @change="onReplaceFileChange" />
            <div v-if="replacePreview" class="replace-preview-wrap">
              <img :src="replacePreview" alt="" />
              <n-button size="tiny" secondary class="replace-repick" @click="triggerReplaceFileSelect">重新选择</n-button>
            </div>
            <div v-else class="replace-pick">
              <n-button size="small" secondary @click="triggerReplaceFileSelect">选择文件</n-button>
            </div>
          </div>
        </div>
        <div v-if="replaceFileObj" class="replace-file-name">已选择：{{ replaceFileObj.name }}</div>
        <n-alert type="info" :bordered="false" style="margin-top: 12px">
          上传的图片将直接替换当前图片内容，资产名称与文件位置保持不变。引用该资产的镜头会被标记为待重做，但不会自动触发任务。
        </n-alert>
      </template>
      <template #footer>
        <n-space justify="end">
          <n-button @click="replaceVisible = false">取消</n-button>
          <n-button type="primary" :loading="replaceBusy" :disabled="!replaceFileObj" @click="confirmReplace">
            确认替换
          </n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 重新生成抽屉：可编辑提示词 + 候选采纳 -->
    <n-drawer v-model:show="regenVisible" :width="'min(560px, 100vw)'" @after-leave="stopRegenPoll">
      <n-drawer-content title="重新生成资产图" closable>
        <template v-if="regenAsset">
          <n-spin v-if="regenLoading" class="page-loading" />
          <n-space v-else vertical :size="12">
            <div class="regen-base">
              <img
                v-if="regenAsset.thumb_url || regenAsset.url"
                :src="regenAsset.thumb_url || regenAsset.url"
                alt=""
                class="regen-base-img"
              />
              <div class="regen-base-info">
                <div class="regen-base-name">{{ regenAsset.name || regenAsset.id }}</div>
                <div class="regen-base-meta">
                  {{ assetTypeLabels[regenAsset.asset_type] || regenAsset.asset_type }} · 当前 v{{ regenAsset.version }}
                </div>
              </div>
            </div>

            <n-alert v-if="regenErrorMsg" type="error" :bordered="false">{{ regenErrorMsg }}</n-alert>

            <div class="regen-field">
              <div class="regen-label">
                提示词
                <span v-if="regenPromptVersion" class="regen-tag">模板 v{{ regenPromptVersion }}</span>
              </div>
              <n-input
                v-model:value="regenPrompt"
                type="textarea"
                :autosize="{ minRows: 6, maxRows: 14 }"
                placeholder="描述该资产的外观与画面要求"
              />
            </div>

            <div class="regen-field">
              <div class="regen-label">负面提示词</div>
              <n-input
                v-model:value="regenNegative"
                type="textarea"
                :autosize="{ minRows: 2, maxRows: 6 }"
                placeholder="不希望出现的元素"
              />
            </div>

            <div class="regen-locked">
              参考图与工作流沿用原生成配置，不可在此修改，以保证资产风格与分镜一致性。
            </div>

            <n-space align="center">
              <n-button type="primary" :loading="regenSubmitting" @click="submitRegenerate">提交重新生成</n-button>
              <n-tag
                v-if="regenJobStatus"
                size="small"
                :type="regenJobStatus === 'succeeded' ? 'success' : (regenJobStatus === 'failed' ? 'error' : 'info')"
              >任务状态：{{ regenJobStatus }}</n-tag>
            </n-space>

            <div class="version-section">
              <div class="section-title">候选版本（采纳后才会替换当前生效版本）</div>
              <n-empty v-if="!regenCandidates.length" size="small" description="暂无候选版本，提交重新生成后在此确认" />
              <div v-else class="candidate-grid">
                <div v-for="c in regenCandidates" :key="c.asset_id" class="candidate-card">
                  <div class="candidate-thumb">
                    <img :src="`/api/assets/${c.asset_id}/file?thumb=true`" alt="" />
                  </div>
                  <div class="candidate-meta">
                    <span class="candidate-ver">v{{ c.version }}</span>
                    <n-button
                      size="tiny"
                      type="primary"
                      :loading="adoptingId === c.asset_id"
                      @click="adoptRegenCandidate(c.asset_id)"
                    >采用</n-button>
                  </div>
                </div>
              </div>
            </div>
          </n-space>
        </template>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
/* 上传替换弹窗 */
.replace-row { display: flex; align-items: center; gap: 12px; }
.replace-card { flex: 1; min-width: 0; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 8px; }
.replace-card-label { font-size: 12px; color: var(--text-secondary, #64748b); margin-bottom: 6px; }
.replace-card-preview { height: 190px; display: flex; align-items: center; justify-content: center; background: var(--bg-elevated, #f8fafc); border-radius: 8px; overflow: hidden; }
.replace-card-preview img { max-width: 100%; max-height: 100%; object-fit: contain; }
.replace-pick { display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
.replace-preview-wrap { position: relative; display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
.replace-repick { position: absolute; bottom: 8px; right: 8px; }
.replace-arrow { font-size: 18px; color: var(--text-secondary, #64748b); }
.replace-file-name { font-size: 12px; color: var(--text-secondary, #64748b); margin-top: 10px; }
/* 重新生成抽屉 */
.regen-base { display: flex; align-items: center; gap: 10px; }
.regen-base-img { width: 88px; height: 88px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border-subtle, #e5e7eb); }
.regen-base-info { min-width: 0; }
.regen-base-name { font-size: 14px; font-weight: 600; color: var(--text-primary, #333); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.regen-base-meta { font-size: 12px; color: var(--text-secondary, #64748b); }
.regen-field { display: flex; flex-direction: column; gap: 6px; }
.regen-label { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); display: flex; align-items: center; gap: 8px; }
.regen-tag { font-size: 11px; font-weight: 400; color: var(--text-secondary, #64748b); }
.regen-locked { font-size: 12px; color: var(--text-secondary, #64748b); background: var(--bg-elevated, #f8fafc); border-radius: 8px; padding: 8px 10px; line-height: 1.6; }
.candidate-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
.candidate-card { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; overflow: hidden; }
.candidate-thumb { height: 120px; background: var(--bg-elevated, #f8fafc); display: flex; align-items: center; justify-content: center; }
.candidate-thumb img { max-width: 100%; max-height: 100%; object-fit: contain; }
.candidate-meta { display: flex; align-items: center; justify-content: space-between; padding: 6px 8px; }
.candidate-ver { font-size: 12px; color: var(--text-secondary, #64748b); }
.readiness-banner { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; background: var(--bg-panel, #fff); }
.readiness-banner.done { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.06); }
.readiness-badge { font-size: 14px; }
.readiness-text { font-size: 13px; color: var(--text-primary, #333); margin-right: auto; }

/* ===== 生成进度面板 ===== */
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
.gen-groups { display: flex; gap: 8px; flex-wrap: wrap; }
.gen-group {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--bg-elevated, #f1f5f9);
  color: var(--text-secondary, #64748b);
}
.gen-group.ok { background: rgba(34, 197, 94, 0.12); color: #15803d; }
.gen-inflight { display: flex; flex-direction: column; gap: 6px; }
.gen-inflight-group { display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap; }
.gen-inflight-kind {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--brand-primary, #6366f1);
  padding-top: 2px;
}
.gen-inflight-items { display: flex; gap: 6px; flex-wrap: wrap; flex: 1; min-width: 0; }
.gen-inflight-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--bg-elevated, #f8fafc);
  border: 1px solid var(--border-subtle, #e5e7eb);
}
.gen-inflight-name { max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gen-inflight-state { color: #b45309; display: inline-flex; align-items: center; gap: 6px; }
.gen-inflight-state.running { color: var(--brand-primary, #6366f1); }
.gen-inflight-state.queued { color: var(--text-secondary, #64748b); }
.gen-mini-bar { width: 54px; }
.gen-done-hint { font-size: 12px; color: #15803d; }
.batch-panel {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-top: 1px dashed var(--border-subtle, #e5e7eb);
  padding-top: 8px;
}
.batch-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.batch-title { font-size: 12px; font-weight: 600; color: var(--text-primary, #333); }
.batch-nums { font-size: 12px; color: var(--text-secondary, #64748b); margin-right: auto; }
.batch-items { display: flex; gap: 6px; flex-wrap: wrap; }
.batch-chip {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--bg-elevated, #f1f5f9);
  color: var(--text-secondary, #64748b);
  border: 1px solid transparent;
}
.batch-chip.running { background: rgba(99, 102, 241, 0.12); color: #4338ca; border-color: rgba(99, 102, 241, 0.3); }
.batch-chip.fail { background: rgba(239, 68, 68, 0.1); color: #b91c1c; border-color: rgba(239, 68, 68, 0.3); }
.gen-failed { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; color: #b91c1c; }
.gen-failed-title { font-weight: 600; }
.gen-failed-item { color: #b45309; }
.craft-card.inflight { border-color: rgba(99, 102, 241, 0.45); background: rgba(99, 102, 241, 0.05); }
.craft-card.failed { border-color: rgba(239, 68, 68, 0.4); }
.assets-page {
  padding: 16px; min-height: calc(100vh - var(--app-header-height, 56px));
  /* 设计令牌（局部，暗色主题变量化预留） */
  --asset-card-radius: 12px;
  --asset-thumb-height: 160px;
  --asset-portrait-thumb-width: 120px;
  --asset-gap: 12px;
  --asset-enter-duration: 0.3s;
}
.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 48px 24px;
}
.state-text {
  margin: 0;
  color: var(--text-secondary);
  font-size: 14px;
}
.assets-layout { display: flex; gap: 16px; }
.category-tree { width: 200px; flex-shrink: 0; }
.category-item {
  padding: 8px 12px; cursor: pointer; border-radius: 8px; font-size: 14px; color: var(--text-secondary);
  transition: all 0.2s; margin-bottom: 4px;
}
.category-item:hover { background: var(--bg-elevated); color: var(--text-primary); }
.category-item.active { background: var(--brand-primary); color: var(--text-on-brand, #fff); }
.assets-main { flex: 1; min-width: 0; }
.craft-section { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; background: var(--bg-panel, #fff); display: flex; flex-direction: column; gap: 10px; }
.craft-card.category-card { cursor: pointer; transition: border-color 0.2s; }
.craft-card.category-card:hover { border-color: var(--brand-primary, #6366f1); }
.craft-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.craft-title { font-size: 14px; font-weight: 600; color: var(--text-primary, #333); }
.craft-sub { font-size: 12px; color: var(--text-secondary, #64748b); margin-right: auto; }
.craft-body { display: flex; gap: 12px; align-items: stretch; flex-wrap: wrap; }
.craft-group { flex: 1 1 260px; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.craft-group-head { display: flex; align-items: center; justify-content: space-between; }
.craft-group-label { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); }
.craft-group-done { font-size: 12px; color: var(--text-secondary, #64748b); }
.craft-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 8px; }
.craft-card { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
.craft-card.done { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.04); }
.craft-card-head { display: flex; align-items: center; gap: 8px; }
.craft-card-name { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.craft-card-prompt { font-size: 12px; color: var(--text-secondary, #64748b); line-height: 1.5; margin: 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.upload-btn { cursor: pointer; }
.drop-zone { min-height: 400px; border: 2px dashed transparent; border-radius: 12px; }
.page-loading { display: flex; justify-content: center; padding: 80px 0; }
.asset-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--asset-gap); }
.asset-groups { display: flex; flex-direction: column; gap: 20px; }
.asset-group { display: flex; flex-direction: column; gap: 8px; }
.asset-group-head { display: flex; align-items: center; gap: 8px; padding-bottom: 4px; border-bottom: 1px solid var(--border-subtle, #e5e7eb); }
.asset-group-label { font-size: 14px; font-weight: 600; color: var(--text-primary, #333); }
.asset-group-count { font-size: 12px; color: var(--text-secondary, #64748b); }
.asset-group-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: var(--asset-gap); }
.asset-card {
  background: var(--bg-panel); border-radius: var(--asset-card-radius); overflow: hidden; cursor: pointer;
  border: 1px solid var(--border-subtle); transition: box-shadow 0.2s; position: relative;
  animation: asset-card-in var(--asset-enter-duration) ease both;
}
.asset-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,0.4); }
@keyframes asset-card-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
/* 角色卡片横向布局：缩略图左 + 信息右 */
.asset-card.is-portrait { display: flex; align-items: stretch; }
.asset-card.is-portrait .asset-thumb { width: var(--asset-portrait-thumb-width); height: auto; flex-shrink: 0; }
.asset-card.is-portrait .asset-thumb img { object-fit: cover; }
.asset-card.is-portrait .asset-info { flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: center; }
.asset-card.is-portrait .asset-actions { position: static; align-self: center; padding: 0 8px 0 0; }
.asset-thumb { width: 100%; height: var(--asset-thumb-height); position: relative; background: var(--bg-elevated); }
.asset-thumb img { width: 100%; height: 100%; object-fit: cover; }
.thumb-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); }
.lock-icon { position: absolute; top: 6px; left: 6px; font-size: 14px; }
.status-dot { position: absolute; bottom: 6px; left: 6px; width: 8px; height: 8px; border-radius: 50%; border: 2px solid var(--bg-panel, #fff); }
.status-dot.ok { background: var(--state-success, #22c55e); }
.status-dot.busy { background: #f0a020; animation: status-pulse 1.2s ease-in-out infinite; }
.status-dot.err { background: #d03050; }
.status-dot.idle { background: #c2c8d0; }
@keyframes status-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.asset-info { padding: 8px 12px; }
.asset-name { font-size: 13px; color: var(--text-primary); margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-meta { display: flex; align-items: center; gap: 8px; }
.asset-actions { position: absolute; bottom: 6px; right: 6px; display: flex; gap: 4px; }
.drawer-preview { margin-bottom: 16px; }
.preview-img { width: 100%; border-radius: 8px; }
.preview-placeholder { width: 100%; height: 200px; background: var(--bg-elevated); border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); }
.field-label { color: var(--text-secondary); font-size: 13px; }
.version-section { margin-top: 8px; }
.section-title { font-size: 14px; color: var(--text-primary); margin-bottom: 8px; font-weight: 500; }
.version-list { display: flex; flex-direction: column; gap: 8px; }
.version-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; background: var(--bg-elevated); border-radius: 8px; }
.version-num { font-size: 13px; color: var(--brand-primary); font-weight: 600; min-width: 32px; }
.version-date { font-size: 12px; color: var(--text-secondary); flex: 1; }
@media (max-width: 768px) {
  .assets-layout { flex-direction: column; }
  .category-tree { width: 100%; display: flex; gap: 8px; overflow-x: auto; }
  .category-item { margin-bottom: 0; white-space: nowrap; }
  .asset-grid, .asset-group-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .craft-grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
}
@media (max-width: 480px) {
  .asset-grid, .asset-group-grid { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); }
}

/* 详情抽屉：大图预览、元信息、提示词与版本记录 */
.drawer-preview-img { width: 100%; max-height: 340px; display: block; }
.drawer-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.drawer-asset-name { font-size: 16px; font-weight: 600; color: var(--text-primary); }
.drawer-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 6px 16px; font-size: 13px; color: var(--text-secondary); }
.drawer-section { border-top: 1px solid var(--border-subtle); padding-top: 12px; }
.drawer-section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.drawer-section-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.drawer-prompt-version { font-size: 12px; color: var(--text-tertiary); }
.drawer-prompt { font-size: 13px; line-height: 1.6; color: var(--text-secondary); background: var(--bg-elevated); border-radius: var(--radius-sm); padding: 10px 12px; white-space: pre-wrap; word-break: break-word; }
.drawer-empty { font-size: 13px; color: var(--text-tertiary); }
</style>
