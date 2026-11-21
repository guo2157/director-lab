<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NTab, NTabs, useMessage } from 'naive-ui'
import {
  listAssets,
  replaceAsset, getAssetRegenerateConfig,
} from '@/api/assets'
import type { Asset } from '@/types/asset'
import { listCharacters, listProps, listScenes } from '@/api/script'
import { generateCharacterPortrait, generatePropAnchor, generateSceneBaseline } from '@/api/storyboard'
import { getAssetGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import { useStageTransition } from '@/composables/useStageTransition'
import StageTransitionDialog from '@/components/project/StageTransitionDialog.vue'
import type { AssetGenerationJob, AssetGenerationProgress } from '@/types/generation'
import VoiceConfigDrawer from '@/components/VoiceConfigDrawer.vue'
import VoiceMatchPanel from '@/components/VoiceMatchPanel.vue'
import { listCharacterVoices } from '@/api/audio'
import AssetStatusBar from '@/components/assets/AssetStatusBar.vue'
import AssetBatchToolbar from '@/components/assets/AssetBatchToolbar.vue'
import AssetCard from '@/components/assets/AssetCard.vue'
import AssetDetailModal from '@/components/assets/AssetDetailModal.vue'
import AssetReplaceModal from '@/components/assets/AssetReplaceModal.vue'
import AssetRegenDrawer from '@/components/assets/AssetRegenDrawer.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import {
  ASSET_KIND_LABELS, ASSET_UNIT_LABELS,
  type AssetCardItem, type AssetKind, type BatchProgressView,
} from '@/components/assets/types'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- 推进闸口（资产库 → 分镜图）---
// 人工审核状态机已取消：不再调审核接口、不再读 assets_review_status。
// 把关动作改为「查后端阶段就绪 → 确认弹窗 → 确认时再查一次 → 跳转」。
// 就绪判定走后端 stage-readiness 的 assets 阶段：检查必需角色/场景/必需道具
// 的图片是否真实生成完成（指针有效 + 版本 current + 文件存在），前端不自己算。
// 不能复用 storyboard_images 阶段——那是镜头级分镜图产物，此刻还没生成。
const stageTransition = useStageTransition({
  projectId: () => projectId.value,
  stage: 'assets',
  to: (pid) => `/project/${pid}/storyboard-images`,
  notReadyTitle: '资产图片尚未全部生成完成',
  confirmTitle: '确认进入分镜图？',
  confirmDescription: [
    '所有必要资产图片已经生成完成。',
    '请确认角色、场景和道具图片均已检查，没有问题，可以开始生成分镜图。',
    '进入下一阶段后，你仍然可以返回资产库替换或重新生成图片。',
  ].join('\n'),
  confirmText: '确认，进入分镜图',
  notify: { warning: (m) => message.warning(m), error: (m) => message.error(m) },
  push: (path) => router.push(path),
})

/** 去分镜图：先查就绪，未就绪提示缺失项；就绪才弹确认框 */
function gotoStoryboardImages() {
  void stageTransition.request()
}

// --- 分类标签页 ---
// 分类值必须与后端权威枚举 enums.ASSET_TYPE 完全一致，否则后端过滤条件永远无匹配
// 资产库只负责项目级公共资产；分镜图/分镜视频属于集级资产，已独立到分镜图与分镜看板页面
const tabs: Array<{ label: string; value: AssetKind | 'voice' }> = [
  { label: '角色', value: 'character' },
  { label: '场景', value: 'scene' },
  { label: '道具', value: 'prop' },
  { label: '音色', value: 'voice' },
]
const activeCategory = ref<AssetKind | 'voice'>('character')
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
      asset_type: (activeCategory.value && activeCategory.value !== 'voice') ? activeCategory.value : undefined,
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

// --- Delete ---
// 卡片上的删除入口已移除（资产库只做生成/替换/重生成），
// 删除能力保留在后端接口，前端不再调用。

// --- 详情弹窗 ---
const detailVisible = ref(false)
const activeAsset = ref<Asset | null>(null)
const detailPrompt = ref('')
const detailPromptVersion = ref('')
/** 提示词来源：edited=用户保存过的编辑值 / template=模板默认值 */
const detailPromptSource = ref<'edited' | 'template'>('template')

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制到剪贴板')
  } catch {
    message.error('复制失败，请手动选择复制')
  }
}

async function openDetail(asset: Asset) {
  activeAsset.value = asset
  detailVisible.value = true
  detailPrompt.value = ''
  detailPromptVersion.value = ''
  detailPromptSource.value = 'template'
  // 可重新生成的资产带出提示词：后端优先回显用户保存过的编辑值，
  // 没编辑过才返回模板默认值，prompt_source 标明是哪种。
  if (canRegenerate(asset)) {
    try {
      const cfg = await getAssetRegenerateConfig(asset.id)
      detailPrompt.value = cfg.prompt || ''
      detailPromptVersion.value = cfg.asset_prompt_version || ''
      detailPromptSource.value = cfg.prompt_source === 'edited' ? 'edited' : 'template'
    } catch {
      detailPrompt.value = ''
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
const replaceBusy = ref(false)

function openReplace(asset: Asset) {
  detailVisible.value = false
  replaceTarget.value = asset
  replaceVisible.value = true
}

async function confirmReplace(file: File) {
  const target = replaceTarget.value
  if (!target) return
  replaceBusy.value = true
  try {
    const res = await replaceAsset(target.id, file)
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

// --- 重新生成（角色/场景/道具：可改提示词，新结果直接覆盖旧版；逻辑在 AssetRegenDrawer 内） ---
const REGEN_TYPES = ['character', 'scene', 'prop']
function canRegenerate(asset: Asset): boolean {
  return REGEN_TYPES.includes(asset.asset_type)
}

const regenVisible = ref(false)
const regenAsset = ref<Asset | null>(null)
/** 详情弹窗带入的提示词（含未保存编辑），作为重新生成抽屉的初值 */
const regenPresetPrompt = ref('')

function openRegenerate(asset: Asset, prompt?: string) {
  detailVisible.value = false
  regenAsset.value = asset
  // 详情页带出的提示词优先，避免抽屉打开后被模板默认值覆盖
  regenPresetPrompt.value = (prompt ?? detailPrompt.value ?? '').trim()
  regenVisible.value = true
}

/** 详情弹窗保存提示词后，本地记录为已编辑态，重新生成沿用这份内容 */
function onDetailPromptSaved(prompt: string) {
  detailPrompt.value = prompt
  detailPromptSource.value = 'edited'
}

// --- 卡片更多操作下拉已移除：资产图只保留替换与重生成，
// 删除入口下线（锁定/解锁此前已随锁定功能一并删除）。

// --- 拖拽资产（供分镜槽位绑定）：拖动逻辑在 AssetCard 内实现 ---

// --- 资产分组展示 ---
/** 按实体名去重：同一实体只保留版本最新的一条，避免历史版本/重复行在同一分类里出现两张卡 */
function dedupeAssetsByName(items: Asset[]) {
  const seen = new Map<string, Asset>()
  for (const a of items) {
    const key = `${a.asset_type}::${a.name || a.id}`
    const prev = seen.get(key)
    if (!prev) {
      seen.set(key, a)
      continue
    }
    // 保留 version 更大者；version 相同时保留 id 字典序更大者（id 后缀单调递增）
    if (a.version > prev.version || (a.version === prev.version && a.id > prev.id)) seen.set(key, a)
  }
  return Array.from(seen.values())
}

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

const { data: charsData, isLoading: charsLoading, isError: charsError } = useQuery({
  queryKey: ['characters', projectId],
  queryFn: () => fetchAllPages((p) => listCharacters(projectId.value, p)),
  enabled: computed(() => !!projectId.value),
})
const { data: scenesData, isLoading: scenesLoading, isError: scenesError } = useQuery({
  queryKey: ['scenes', projectId],
  queryFn: () => fetchAllPages((p) => listScenes(projectId.value, { ...p, include_shots: false })),
  enabled: computed(() => !!projectId.value),
})
const { data: propsData, isLoading: propsLoading, isError: propsError } = useQuery({
  queryKey: ['props', projectId],
  queryFn: () => fetchAllPages((p) => listProps(projectId.value, p)),
  enabled: computed(() => !!projectId.value),
})
const readiness = computed(() => {
  const cs = charsData.value?.items ?? []
  const ss = (scenesData.value?.items ?? []) as Array<{ baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ anchor_asset_id?: string | null }>
  // 与页面展示口径保持一致：卡片网格列出全部角色/场景/道具，齐全度亦按全部统计
  const total = cs.length + ss.length + ps.length
  const done =
    cs.filter((c) => !!c.portrait_asset_id).length +
    ss.filter((s) => !!s.baseline_asset_id).length +
    ps.filter((p) => !!p.anchor_asset_id).length
  return { total, done, all: total > 0 && done === total }
})

// --- 音色配置抽屉（音频模块改造：角色资产增加音色配置入口） ---
const voiceDrawerShow = ref(false)
const voiceDrawerCharacterId = ref<string | null>(null)
const voiceDrawerCharacterName = ref('')
function openVoiceConfig(it: { id: string; name: string }) {
  voiceDrawerCharacterId.value = it.id
  voiceDrawerCharacterName.value = it.name
  voiceDrawerShow.value = true
}
/** 角色实体按名称索引（资产表不存 entity_id，name 即实体名） */
const characterByName = computed(() => {
  const map = new Map<string, { id: string; name: string }>()
  for (const c of (charsData.value?.items ?? []) as Array<{ id: string; name: string }>) {
    map.set(c.name, c)
  }
  return map
})
function characterOfName(name?: string) {
  if (!name) return null
  return characterByName.value.get(name) ?? null
}
function openVoiceConfigOfAsset(asset: { name?: string }) {
  const ch = characterOfName(asset.name)
  if (!ch) {
    message.warning('未匹配到对应角色实体，请稍后重试')
    return
  }
  openVoiceConfig({ id: ch.id, name: ch.name })
}
/** 音色变更后递增，用于触发卡片音色名重新拉取 */
const voiceQuerySeed = ref(0)
/** 角色音色名映射：按角色实体 ID 聚合，供卡片音色条展示当前音色 */
const voiceNameQuery = useQuery({
  queryKey: computed(() => ['character-voices', projectId.value, voiceQuerySeed.value]),
  queryFn: async () => {
    const cs = (charsData.value?.items ?? []) as Array<{ id: string }>
    const map: Record<string, string> = {}
    await Promise.all(
      cs.map(async (c) => {
        try {
          const res = await listCharacterVoices(c.id)
          const voices = res?.voices ?? []
          const picked =
            voices.find((v) => v.is_default && v.enabled) ??
            voices.find((v) => v.is_default) ??
            voices.find((v) => v.enabled) ??
            voices[0]
          if (picked) map[c.id] = picked.voice_name || picked.voice_id
        } catch {
          /* 单个角色查询失败不影响其它卡片展示 */
        }
      }),
    )
    return map
  },
  enabled: computed(() => !!projectId.value && (charsData.value?.items ?? []).length > 0),
})
function voiceNameOf(name?: string) {
  const ch = characterOfName(name)
  if (!ch) return ''
  return voiceNameQuery.data.value?.[ch.id] || ''
}
/** 音色抽屉关闭后刷新卡片音色名 */
watch(voiceDrawerShow, (show) => {
  if (show) return
  voiceQuerySeed.value += 1
  queryClient.invalidateQueries({ queryKey: ['character-voices', projectId.value] })
})

// --- 生成进度（后端聚合：正在生成什么 / 批量进度 / 是否全部完成） ---
/** 本次会话提交的资产生成任务：用于「刚才这批」的进度条与失败重试 */
interface SubmittedAsset {
  id: string
  name: string
  kind: AssetKind
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
    message.success('公共资产图已全部生成完成，检查效果后即可进入分镜图')
  },
})

/** 在途任务 → 卡片可直接查询的状态 */
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
/** 项目级齐全度（后端权威口径，不受标签页切换影响）：状态条与审核闸口都用它 */
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

// --- 制作资产（公共资产生成：角色定妆图/场景基准图/道具锚定图） ---
const craftingId = ref<string | null>(null)
const batchCrafting = ref(false)
/**
 * 资产出图工作流（asset_image 槽位）。
 * 角色定妆图 / 场景基准图 / 道具锚定图共用这一份选择：只允许纯文生图工作流，
 * 候选由后端 /workflows/candidates 按真实能力过滤，前端不硬编码描述与 ID。
 * 未选择时后端按项目默认 → 系统默认自行解析，行为与改造前一致。
 */
const assetImageWorkflowId = ref<string | undefined>(undefined)
// --- 制作资产勾选批量生成：勾选多个/全选后批量触发 ---
const selectedCraftIds = ref<string[]>([])
function toggleCraft(id: string, checked: boolean) {
  const set = new Set(selectedCraftIds.value)
  if (checked) set.add(id)
  else set.delete(id)
  selectedCraftIds.value = [...set]
}
const genPortrait = useMutation({
  mutationFn: (id: string) =>
    generateCharacterPortrait(id, { workflow_id: assetImageWorkflowId.value }),
  onSuccess: () => {
    message.success('角色定妆图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['characters', projectId] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
const genBaseline = useMutation({
  mutationFn: (id: string) =>
    generateSceneBaseline(id, { workflow_id: assetImageWorkflowId.value }),
  onSuccess: () => {
    message.success('场景基准图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})
const genAnchor = useMutation({
  mutationFn: (id: string) =>
    generatePropAnchor(id, { workflow_id: assetImageWorkflowId.value }),
  onSuccess: () => {
    message.success('道具锚定图任务已提交')
    queryClient.invalidateQueries({ queryKey: ['props', projectId] })
  },
  onError: (e: any) => message.error(`生成失败: ${e?.message ?? e}`),
})

interface CraftItem {
  id: string
  name: string
  prompt: string
  done: boolean
  kind: AssetKind
}
const craftGroups = computed(() => {
  const cs = (charsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; portrait_asset_id?: string | null }>
  const ss = (scenesData.value?.items ?? []) as Array<{ id: string; title?: string; description?: string; baseline_asset_id?: string | null }>
  const ps = (propsData.value?.items ?? []) as Array<{ id: string; name: string; description?: string; anchor_asset_id?: string | null }>
  return [
    {
      type: 'character' as AssetKind, label: '角色', unit: '定妆图',
      items: cs.map((c) => ({ id: c.id, name: c.name, prompt: c.description ?? '', done: !!c.portrait_asset_id, kind: 'character' as const })) as CraftItem[],
    },
    {
      type: 'scene' as AssetKind, label: '场景', unit: '基准图',
      items: ss.map((s) => ({ id: s.id, name: s.title ?? s.id, prompt: s.description ?? '', done: !!s.baseline_asset_id, kind: 'scene' as const })) as CraftItem[],
    },
    {
      type: 'prop' as AssetKind, label: '道具', unit: '锚定图',
      items: ps.map((p) => ({ id: p.id, name: p.name, prompt: p.description ?? '', done: !!p.anchor_asset_id, kind: 'prop' as const })) as CraftItem[],
    },
  ]
})

/** 每个标签页的实体数量（标签上的计数，一眼看出这一分类有几件事要做） */
const tabCounts = computed<Record<string, number>>(() => {
  const map: Record<string, number> = {}
  for (const g of craftGroups.value) map[g.type] = g.items.length
  return map
})

/** 资产按「类型::名称」索引：卡片用图片、详情、替换、重生成，都基于这一张表 */
const assetByKey = computed(() => {
  const map = new Map<string, Asset>()
  for (const a of dedupeAssetsByName(assetsQuery.data.value?.items || [])) {
    map.set(`${a.asset_type}::${a.name || a.id}`, a)
  }
  return map
})

/** 当前标签的卡片：一个实体一张卡（实体描述 + 已绑定资产），再补上没有对应实体的历史资产 */
const cardItems = computed<AssetCardItem[]>(() => {
  const tab = activeCategory.value
  if (tab === 'voice') return []
  const group = craftGroups.value.find((g) => g.type === tab)
  const items: AssetCardItem[] = []
  for (const it of group?.items ?? []) {
    items.push({
      key: `${tab}:${it.id}`,
      kind: tab,
      entityId: it.id,
      name: it.name,
      desc: it.prompt,
      done: it.done,
      asset: assetByKey.value.get(`${tab}::${it.name}`) ?? null,
    })
  }
  const entityNames = new Set((group?.items ?? []).map((i) => i.name))
  for (const asset of assetByKey.value.values()) {
    if (asset.asset_type !== tab) continue
    if (entityNames.has(asset.name || '')) continue
    items.push({
      key: `${tab}:asset:${asset.id}`,
      kind: tab,
      entityId: null,
      name: asset.name || asset.id,
      desc: '',
      done: true,
      asset,
    })
  }
  return items
})

/** 只有绑定了脚本实体的卡片能勾选批量生成（孤立资产只能单张替换/重生成） */
const selectableIds = computed(() => cardItems.value.filter((i) => i.entityId).map((i) => i.entityId as string))
function toggleAll(checked: boolean) {
  const set = new Set(selectedCraftIds.value)
  for (const id of selectableIds.value) {
    if (checked) set.add(id)
    else set.delete(id)
  }
  selectedCraftIds.value = [...set]
}
/** 切换标签页时丢弃上一个标签的勾选，避免「已选 3 个」与实际可见卡片不符 */
watch(activeCategory, () => {
  selectedCraftIds.value = []
})

const scopeLabel = computed(() => {
  const tab = activeCategory.value
  if (tab === 'voice') return ''
  return `${ASSET_KIND_LABELS[tab]}${ASSET_UNIT_LABELS[tab]}`
})

/** 本批进度：工具栏内联展示 */
const batchView = computed<BatchProgressView | null>(() => {
  const b = batchProgress.value
  if (!b) return null
  return { total: b.total, done: b.done, running: b.running, failed: b.failed, percent: b.percent }
})
/** 当前标签下失败的实体数：决定是否露出「重试」 */
const failedInTab = computed(
  () => cardItems.value.filter((i) => i.entityId && failedEntityIds.value.has(i.entityId)).length,
)

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
  const picked = craftGroups.value
    .flatMap((g) => g.items)
    .filter((i) => selectedCraftIds.value.includes(i.id))
  if (!picked.length) {
    message.warning('请先勾选要生成的资产')
    return
  }
  /**
   * 幂等：已生成过 / 正在生成中的资产直接跳过，不重新提交。
   *
   * 旧实现无脑逐个提交，用户点「全选」→「批量生成」时，
   * 已生成好的定妆图/基准图/锚定图被全部重做，覆盖掉人工挑过的结果，
   * 还白烧 GPU。后端单实体接口（/characters/{id}/generate 等）只查在途任务、
   * 不查已生成，所以必须在提交前用 `done` 过滤。
   *
   * 在途过滤（2026-11-21 补充）：只过滤 done 不够——任务还在排队/生成中时
   * `done` 仍是 false，重复点击会为同一实体反复建任务。实测事故：分镜图页
   * 6 个在途镜头再点一次批量生成，任务数从 11 变 17。
   *
   * 需要重做时走卡片上的「重生成」——那是明确的主动意图，不受此限制。
   */
  const todo = picked.filter((i) => !i.done && !inflightById.value.has(i.id))
  const skippedDone = picked.filter((i) => i.done).length
  const skippedInflight = picked.filter(
    (i) => !i.done && inflightById.value.has(i.id),
  ).length
  if (!todo.length) {
    if (skippedInflight && !skippedDone) {
      message.info(`勾选的 ${skippedInflight} 个资产正在生成中，请等待完成后再操作`)
    } else {
      message.info(`勾选的 ${picked.length} 个资产均已生成，无需重复生成；如需重做请用卡片上的「重生成」`)
    }
    return
  }
  batchCrafting.value = true
  const submittedItems: CraftItem[] = []
  const jobIds: string[] = []
  const failed: string[] = []
  try {
    for (const it of todo) {
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
      message.success(
        skippedDone
          ? `已提交 ${submittedItems.length} 个图生成任务（跳过 ${skippedDone} 个已生成的），进度见上方工具栏`
          : `已提交 ${submittedItems.length} 个图生成任务，进度见上方工具栏`,
      )
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
  const all = craftGroups.value.flatMap((g) => g.items)
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

// --- 加载 / 错误 / 空态 ---
const loading = computed(
  () => assetsQuery.isLoading.value || charsLoading.value || scenesLoading.value || propsLoading.value,
)
const loadError = computed(() => {
  if (assetsQuery.isError.value) return errMsg(assetsQuery.error.value)
  if (charsError.value || scenesError.value || propsError.value) return '角色/场景/道具数据加载失败，请重试'
  return ''
})
async function retryLoad() {
  await Promise.all([
    assetsQuery.refetch(),
    queryClient.refetchQueries({ queryKey: ['characters', projectId] }),
    queryClient.refetchQueries({ queryKey: ['scenes', projectId] }),
    queryClient.refetchQueries({ queryKey: ['props', projectId] }),
  ])
}

const activeKindLabel = computed(() =>
  activeCategory.value === 'voice' ? '音色' : ASSET_KIND_LABELS[activeCategory.value],
)
const emptyTitle = computed(() =>
  readiness.value.total ? `暂无${activeKindLabel.value}` : '还没有可制作的资产',
)
const emptyDesc = computed(() =>
  readiness.value.total
    ? '该分类下还没有实体，回到解析页补充后再回来生成。'
    : '解析完成并确认角色、场景、道具后，这里会按分类列出每个资产与成品图。',
)

// --- 卡片交互 ---
function onCardOpen(item: AssetCardItem) {
  if (item.asset) void openDetail(item.asset)
}
function onCardGenerate(item: AssetCardItem) {
  const entityId = item.entityId
  const craft = craftGroups.value.flatMap((g) => g.items).find((i) => i.id === entityId)
  if (craft) void handleCraft(craft)
  else message.warning('该资产没有对应的脚本实体，请使用重新生成')
}
function onCardReplace(item: AssetCardItem) {
  if (item.asset) openReplace(item.asset)
}
function onCardRegenerate(item: AssetCardItem) {
  if (item.asset) openRegenerate(item.asset)
}
function onCardVoice(item: AssetCardItem) {
  if (item.kind === 'character') openVoiceConfigOfAsset({ name: item.name })
}

const craftingIdFor = (item: AssetCardItem) => craftingId.value === item.entityId

onMounted(() => {
  // 刷新页面/再次进入时也要能看到当前生成进度（有在途任务才轮询）
  void assetProgress.bootstrap()
})
</script>

<template>
  <div class="page assets-page">
    <!-- 左侧「制作流程」栏已高亮 02 资产库，页内不再重复同名大标题。
         H1 必须保留（.visually-hidden）：删掉 PageHeader 时曾把 H1 一并丢掉，
         导致本页在无障碍树里没有任何标题层级。 -->
    <h1 class="visually-hidden">资产库</h1>
    <!-- 顶部两卡：执行进度（只读）+ 批量生成（可写） -->
    <AssetStatusBar
      :done="projectReadiness.done"
      :total="projectReadiness.total"
      :all-done="projectReadiness.all"
      :inflight-count="assetProgress.inflightCount.value"
      :loading="assetProgress.loading.value"
      @next="gotoStoryboardImages"
      @refresh="assetProgress.refresh()"
    />

    <!-- 批量操作：固定在标签页上方，滚动时不跑掉 -->
    <AssetBatchToolbar
      v-if="activeCategory !== 'voice'"
      :scope-label="scopeLabel"
      :total="selectableIds.length"
      :selected="selectedCraftIds.length"
      :batch-loading="batchCrafting"
      :inflight-count="assetProgress.inflightCount.value"
      :batch="batchView"
      :retrying="retryingFailed"
      :failed-count="failedInTab"
      :workflow-id="assetImageWorkflowId"
      @update:workflow-id="(v: string | undefined) => (assetImageWorkflowId = v)"
      @toggle-all="toggleAll"
      @batch-generate="handleBatchCraft"
      @retry-failed="retryFailedCraft"
      @clear-batch="clearBatch"
    />

    <n-tabs
      :value="activeCategory"
      type="line"
      size="medium"
      class="assets-tabs"
      @update:value="(v: string) => (activeCategory = v as AssetKind | 'voice')"
    >
      <n-tab v-for="t in tabs" :key="t.value" :name="t.value">
        {{ t.label }}<span v-if="t.value !== 'voice'" class="tab-count">{{ tabCounts[t.value] ?? 0 }}</span>
      </n-tab>
    </n-tabs>

    <!-- 音色：匹配面板自带流程，不叠加卡片网格 -->
    <VoiceMatchPanel v-if="activeCategory === 'voice'" :project-id="projectId" />

    <ErrorRetry
      v-else-if="loadError"
      title="资产加载失败"
      :message="loadError"
      @retry="retryLoad"
    />

    <SkeletonList
      v-else-if="loading"
      variant="card"
      :rows="6"
      :columns="4"
      height="220px"
    />

    <EmptyState
      v-else-if="!cardItems.length"
      :title="emptyTitle"
      :description="emptyDesc"
      :action-text="readiness.total ? '' : '去解析页'"
      @action="router.push(`/project/${projectId}/parse`)"
    />

    <div v-else class="card-grid">
      <AssetCard
        v-for="item in cardItems"
        :key="item.key"
        :item="item"
        :selectable="!!item.entityId"
        :selected="selectedCraftIds.includes(item.entityId ?? '')"
        :inflight-text="item.entityId ? inflightText(inflightById.get(item.entityId)) : ''"
        :failed="!!item.entityId && failedEntityIds.has(item.entityId)"
        :voice-name="item.kind === 'character' ? voiceNameOf(item.name) : ''"
        :class="{ 'is-busy': craftingIdFor(item) }"
        @open="onCardOpen(item)"
        @generate="onCardGenerate(item)"
        @replace="onCardReplace(item)"
        @regenerate="onCardRegenerate(item)"
        @voice="onCardVoice(item)"
        @update:selected="(v: boolean) => item.entityId && toggleCraft(item.entityId, v)"
      />
    </div>

    <!-- 资产详情（点卡片才出现） -->
    <AssetDetailModal
      v-model:show="detailVisible"
      :asset="activeAsset"
      :prompt="detailPrompt"
      :prompt-version="detailPromptVersion"
      :prompt-source="detailPromptSource"
      @replace="activeAsset && openReplace(activeAsset)"
      @regenerate="(p: string) => activeAsset && openRegenerate(activeAsset, p)"
      @prompt-saved="onDetailPromptSaved"
      @copy="copyText"
    />

    <!-- 上传替换 -->
    <AssetReplaceModal
      v-model:show="replaceVisible"
      :target="replaceTarget"
      :busy="replaceBusy"
      @confirm="confirmReplace"
    />

    <!-- 重新生成：提示词可改，新结果直接覆盖旧版，只保留一份 -->
    <AssetRegenDrawer
      v-model:show="regenVisible"
      :workflow-id="assetImageWorkflowId"
      :asset="regenAsset"
      :preset-prompt="regenPresetPrompt"
      @refresh="refreshAll"
      @submitted="assetProgress.start({ notifyOnComplete: true })"
    />

    <!-- 阶段跳转确认弹窗：就绪时确认进入分镜图，未就绪时展示缺失资产 -->
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

    <!-- 音色配置抽屉（音频模块改造） -->
    <VoiceConfigDrawer
      v-model:show="voiceDrawerShow"
      :character-id="voiceDrawerCharacterId"
      :character-name="voiceDrawerCharacterName"
    />
  </div>
</template>

<style scoped>
.assets-page {
  min-width: 0;
}
.assets-tabs {
  margin-bottom: var(--sp-4);
}
.tab-count {
  margin-left: var(--sp-2);
  padding: 0 var(--sp-1);
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-tertiary);
  background: var(--muted-soft);
  border-radius: var(--radius-sm);
}
/* 网格列宽：保持 auto-fill + 1fr。
   曾试过 `minmax(220px, 340px)` + justify-content:start，想让 2 个角色时单卡
   从 278px 涨到 340px，但实测 1440 与 1920 都只算出 3 列（列数由 min 值决定后
   再被 max 截断，行为不可预测），右侧空白仅从 588 收到 464，收益不足。
   右侧空白真正的成因是「角色 tab 只有 2 项」——场景 5 项、道具 4 项时 4 列是
   正确密度。按数据量调列宽会让同一页在不同 tab 下跳动，不做。 */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--sp-4);
}
@media (max-width: 480px) {
  .card-grid {
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: var(--sp-3);
  }
}
</style>
