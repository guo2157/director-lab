<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpace, NSpin, NEmpty, NTag, NDrawer, NDrawerContent,
  NInput, NTooltip, NModal, NAlert, NCheckbox, useMessage, useDialog,
  NImage, NDropdown, NTimeline, NTimelineItem,
} from 'naive-ui'
import {
  listAssets, deleteAsset, lockAsset, unlockAsset, listAssetVersions,
  replaceAsset, regenerateAsset, getAssetRegenerateConfig,
} from '@/api/assets'
import type { AssetRegenerateConfig } from '@/api/assets'
import type { Asset, AssetVersion } from '@/types/asset'
import { fetchCandidates, adoptCandidate } from '@/api/regeneration'
import type { CandidateAsset } from '@/api/regeneration'
import { getJob } from '@/api/jobs'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { getProject, reviewProjectAssets } from '@/api/projects'
import { generateCharacterPortrait, generatePropAnchor, generateSceneBaseline } from '@/api/storyboard'

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
const versions = ref<AssetVersion[]>([])
const loadingVersions = ref(false)
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

function formatTime(ts?: string | null): string {
  if (!ts) return '-'
  try {
    return new Date(ts).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return ts
  }
}

async function openDrawer(asset: Asset) {
  activeAsset.value = asset
  drawerVisible.value = true
  loadingVersions.value = true
  drawerPrompt.value = ''
  drawerPromptVersion.value = ''
  try {
    const res = await listAssetVersions(asset.id)
    versions.value = res
  } catch (e: any) {
    message.error(`加载版本失败: ${e?.message ?? e}`)
  } finally {
    loadingVersions.value = false
  }
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
    message.success('重新生成任务已提交')
    startRegenPoll(res.job_id)
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
  return defs.map((d) => ({ ...d, items: items.filter((a) => a.asset_type === d.type) }))
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
  // 道具只统计需要锚定图的条目，与后端审核通过时的校验口径保持一致（anchor_required=true）
  const ps = (propsData.value?.items ?? []) as Array<{ anchor_required?: boolean; anchor_asset_id?: string | null }>
  const requiredProps = ps.filter((p) => p.anchor_required)
  const total = cs.length + ss.length + requiredProps.length
  const done =
    cs.filter((c) => !!c.portrait_asset_id).length +
    ss.filter((s) => !!s.baseline_asset_id).length +
    requiredProps.filter((p) => !!p.anchor_asset_id).length
  return { total, done, all: total > 0 && done === total }
})
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
  const ss = (scenesData.value?.items ?? []) as Array<{ id: string; title?: string; summary?: string; baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; anchor_asset_id?: string | null }>
  return [
    {
      type: 'character', label: '角色', unit: '定妆图',
      items: cs.map((c) => ({ id: c.id, name: c.name, prompt: c.description ?? '', done: !!c.portrait_asset_id, kind: 'character' as const })) as CraftItem[],
    },
    {
      type: 'scene', label: '场景', unit: '基准图',
      items: ss.map((s) => ({ id: s.id, name: s.title ?? s.id, prompt: s.summary ?? '', done: !!s.baseline_asset_id, kind: 'scene' as const })) as CraftItem[],
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
function handleCraft(it: CraftItem) {
  craftingId.value = it.id
  const onSettled = () => { craftingId.value = null }
  if (it.kind === 'character') genPortrait.mutate(it.id, { onSettled })
  else if (it.kind === 'scene') genBaseline.mutate(it.id, { onSettled })
  else genAnchor.mutate(it.id, { onSettled })
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
  let submitted = 0
  const failed: string[] = []
  try {
    for (const it of picked) {
      try {
        if (it.kind === 'character') await genPortrait.mutateAsync(it.id)
        else if (it.kind === 'scene') await genBaseline.mutateAsync(it.id)
        else await genAnchor.mutateAsync(it.id)
        submitted += 1
      } catch {
        failed.push(it.name)
      }
    }
    if (submitted) message.success(`已提交 ${submitted} 个资产生成任务，生成进度见齐全度横幅`)
    if (failed.length) message.error(`${failed.length} 个资产提交失败：${failed.slice(0, 3).join('、')}`)
    selectedCraftIds.value = []
  } finally {
    batchCrafting.value = false
  }
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
        <!-- 资产齐全度横幅 -->
        <div class="readiness-banner" :class="{ done: readiness.all }">
          <span class="readiness-badge">{{ readiness.all ? '✓' : '⏳' }}</span>
          <span class="readiness-text">
            {{ readiness.all ? '公共资产图已齐全，请完成人工审核后进入分镜图' : `资产图生成中：${readiness.done}/${readiness.total} 已完成` }}
          </span>
          <n-button size="small" quaternary @click="router.push(`/project/${projectId}/parse`)">返回解析页</n-button>
        </div>
        <!-- 人工审核闸口：资产库出图完毕后必须人工确认，通过后才放行分镜图 -->
        <div class="readiness-banner" :class="{ done: assetsReviewStatus === 'approved' }">
          <span class="readiness-badge">{{ assetsReviewStatus === 'approved' ? '✓' : '⏳' }}</span>
          <span class="status-tag" :class="reviewStatusTagClass(assetsReviewStatus)">{{ reviewStatusTagLabel(assetsReviewStatus) }}</span>
          <span class="readiness-text">{{ reviewStatusText }}</span>
          <n-button
            size="small"
            type="primary"
            :disabled="!readiness.all"
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
            <span class="craft-sub">角色定妆图 / 场景基准图 / 道具锚定图（项目级公共资产，按名称绑定供分镜引用）</span>
            <n-button size="medium" type="primary" :loading="batchCrafting" :disabled="!selectedCraftIds.length" @click="handleBatchCraft">批量生成（{{ selectedCraftIds.length }}）</n-button>
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
              <div v-for="it in grp.items" :key="it.id" class="craft-card" :class="{ done: it.done }">
                <div class="craft-card-head">
                  <n-checkbox
                    :checked="selectedCraftIds.includes(it.id)"
                    @update:checked="(v: boolean) => toggleCraft(it.id, v)"
                    @click.stop
                  />
                  <span class="craft-card-name">{{ it.name }}</span>
                  <n-tag v-if="it.done" size="tiny" type="success" :bordered="false">已生成</n-tag>
                  <n-button v-else size="tiny" quaternary type="primary" :loading="craftingId === it.id" @click="handleCraft(it)">生成{{ grp.unit }}</n-button>
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
                <img v-if="asset.thumb_url || asset.url" :src="asset.thumb_url || asset.url" alt="" />
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
              :src="activeAsset.url"
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
              <div><span class="field-label">当前版本：</span>v{{ activeAsset.version }}</div>
              <div><span class="field-label">更新时间：</span>{{ formatTime(activeAsset.updated_at) }}</div>
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
            <div class="drawer-section">
              <div class="drawer-section-head">
                <span class="drawer-section-title">版本记录</span>
                <n-spin v-if="loadingVersions" :size="14" />
              </div>
              <n-timeline v-if="versions.length" size="medium">
                <n-timeline-item
                  v-for="v in versions"
                  :key="v.version"
                  :title="`v${v.version}`"
                  :content="formatTime(v.created_at)"
                  :type="v.version === activeAsset.version ? 'success' : 'default'"
                />
              </n-timeline>
              <div v-else-if="!loadingVersions" class="drawer-empty">暂无版本记录</div>
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
