<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpin, NEmpty, NTag,
  NInput, NTooltip, NCheckbox, NModal, NRadioGroup, NRadio, NSelect, NProgress, NSwitch, useMessage,
} from 'naive-ui'
import { listCharacters, listProps, listScenes, listSceneShots, reorderSceneShots, updateShot } from '@/api/script'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { listJobs } from '@/api/jobs'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import {
  fetchRegenerateConfig,
  fetchRegenerateWorkspace,
  saveRegenerateWorkspace,
  submitRegenerateWorkspace,
  type WorkspaceReference,
} from '@/api/regeneration'
import { listWorkflows } from '@/api/workflows'
import { getClipGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { Scene, Shot, ShotKind } from '@/types/script'
import type { ClipGenerationJob, ClipGenerationProgress } from '@/types/generation'
import type { GenerateShotPayload } from '@/types/job'
import ShotAudioPanel from '@/components/ShotAudioPanel.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- 视频生成档位：极速 5s / 标准 5s / 质量 10s（对应库里三套 4 步工作流） ---
const clipTier = ref<'fast_5s' | 'std_5s' | 'quality_10s'>('std_5s')
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



// --- Single generate ---
const generateMutation = useMutation({
  mutationFn: (params: { shotId: string; payload: GenerateShotPayload }) =>
    generateShot(params.shotId, params.payload),
  onSuccess: () => {
    message.success('生成任务已提交，正在生成（进度见顶部进度面板）')
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
    queryClient.invalidateQueries({ queryKey: ['jobs', projectId, 'generate_clip'] })
    void trackClipProgress()
  },
  onError: (e: any) => {
    message.error(`生成失败: ${e?.message ?? e}`)
  },
})

/** 抽屉内生成：携带编辑后的提示词（后端 params.prompt 覆盖默认值，@角色/@场景 自动注入参考图） */
function handleGenerateFromDrawer(target: 'keyframe' | 'clip') {
  if (!activeShot.value) return
  const params: Record<string, string> = {}
  // 分镜图提示词不再在视频看板编辑，生成分镜图时使用镜头自身的 image_prompt
  if (target === 'clip') {
    if (editVideoPrompt.value) params.prompt = editVideoPrompt.value
    // 负向提示词仅在当前工作流声明支持时传入，避免向不支持的节点强塞参数
    if (negativeSupported.value && editNegativePrompt.value.trim()) {
      params.negative_prompt = editNegativePrompt.value.trim()
    }
  }
  generateMutation.mutate({
    shotId: activeShot.value.id,
    payload: { target, params, workflow_id: target === 'clip' ? clipWorkflowId.value : undefined } as GenerateShotPayload,
  })
}

// --- 重新生成分镜视频：直接复用详情页 H3 提示词与参考图，生成的视频替换原视频 ---
const directRegenLoading = ref(false)

// --- 镜头详情内嵌参考图绑定管理 ---
const refBindingsLoading = ref(false)
const refBindingsSaving = ref(false)
const workspaceBindings = ref<WorkspaceReference[]>([])
const workspaceSlots = ref<{ param: string; node_id?: string | null; input_key?: string | null; image_no?: number | null; required?: boolean | null }[]>([])
const chainFrameStatus = ref<'ready' | 'missing' | 'not_applicable' | null>(null)

async function loadRefBindings(shotId: string) {
  refBindingsLoading.value = true
  try {
    const ws = await fetchRegenerateWorkspace(shotId, 'clip')
    workspaceBindings.value = ws.references ?? []
    chainFrameStatus.value = ws.chain_frame?.status ?? null
    // 档位跟随该镜头实际使用的工作流，避免打开详情后被默认档位覆盖
    const wfName = ws.workflow?.name ?? ''
    const tier = (['fast_5s', 'std_5s', 'quality_10s'] as const).find((t) => wfName.endsWith(t))
    if (tier) clipTier.value = tier
    workspaceSlots.value = (ws.available_slots ?? []).map((s) => ({
      param: s.param,
      node_id: s.node_id,
      input_key: s.input_key,
      image_no: s.image_no,
      required: s.required,
    }))
  } catch {
    workspaceBindings.value = []
    workspaceSlots.value = []
    chainFrameStatus.value = null
  } finally {
    refBindingsLoading.value = false
  }
}

const unboundRefSlots = computed(() => {
  const bound = new Set(workspaceBindings.value.map((r) => r.slot_id))
  return workspaceSlots.value.filter((s) => !bound.has(s.param))
})

function addRefBinding(slotParam: string) {
  const slot = workspaceSlots.value.find((s) => s.param === slotParam)
  if (!slot) return
  workspaceBindings.value = [
    ...workspaceBindings.value,
    {
      reference_id: `ref_${slotParam}_${Date.now()}`,
      slot_id: slotParam,
      node_id: slot.node_id,
      input_key: slot.input_key,
      image_no: slot.image_no ?? null,
      asset_id: null,
      enabled: true,
      required: Boolean(slot.required),
      status: 'pending_confirmation',
    } as WorkspaceReference,
  ]
}

function removeRefBinding(referenceId: string) {
  const ref = workspaceBindings.value.find((r) => r.reference_id === referenceId)
  if (ref?.required) {
    message.warning('该槽位为工作流必需输入，不可移除，只能替换资产或关闭启用开关')
    return
  }
  workspaceBindings.value = workspaceBindings.value.filter((r) => r.reference_id !== referenceId)
}

const shotAssetOptions = computed(() => {
  const opts: { label: string; value: string }[] = []
  const chars = (charsData.value?.items ?? []) as Array<{ id: string; name: string; portrait_asset_id?: string | null }>
  const scenes = (scenesQuery.data.value?.items ?? []) as Array<{ id: string; title: string; baseline_asset_id?: string | null }>
  const props = (propsData.value?.items ?? []) as Array<{ id: string; name: string; anchor_asset_id?: string | null }>
  for (const c of chars) {
    if (c.portrait_asset_id) opts.push({ label: `${c.name}（定妆）`, value: c.portrait_asset_id })
  }
  for (const s of scenes) {
    if (s.baseline_asset_id) opts.push({ label: `${s.title}（场景）`, value: s.baseline_asset_id })
  }
  for (const p of props) {
    if (p.anchor_asset_id) opts.push({ label: `${p.name}（道具）`, value: p.anchor_asset_id })
  }
  if (activeShot.value?.current_assets?.keyframe?.asset_id) {
    opts.push({ label: '本镜分镜图', value: activeShot.value.current_assets.keyframe.asset_id })
  }
  // 已绑定但不在上述选项里的资产（如上一镜尾帧）也要有可读名称，否则选择框只显示资产 ID
  for (const ref of workspaceBindings.value) {
    if (ref.asset_id && ref.asset_name && !opts.some((o) => o.value === ref.asset_id)) {
      opts.push({ label: ref.asset_name, value: ref.asset_id })
    }
  }
  return opts
})

async function saveRefBindings() {
  if (!activeShot.value) return
  refBindingsSaving.value = true
  try {
    const payload = {
      target: 'clip',
      references: workspaceBindings.value,
      character_bindings: [],
      spatial_layout: { characters: [] },
      action_timeline: { actions: [] },
      prop_bindings: [],
      prompt_sections: {},
      final_prompt: '',
      parameters: {},
    }
    await saveRegenerateWorkspace(activeShot.value.id, payload)
    message.success('参考图绑定已保存')
    await loadRefBindings(activeShot.value.id)
  } catch (e: unknown) {
    message.error(`保存失败：${(e as Error)?.message ?? e}`)
  } finally {
    refBindingsSaving.value = false
  }
}


/** 该镜头是否已有视频产物（含正在生成的在途任务）：有则可视为『重新生成』 */
const clipActionIsRegenerate = computed(() => {
  const shot = activeShot.value
  if (!shot) return false
  if (shot.current_assets?.clip) return true
  return activeClipJobByShot.value.has(shot.id)
})
const clipActionLabel = computed(() =>
  clipActionIsRegenerate.value ? '重新生成' : '生成视频',
)
/**
 * 重新生成视频：不再弹工作台，直接复用详情页当前的 H3 提示词与参考图绑定提交。
 * 生成成功后由执行器把新视频置为 current，原视频转历史版本。
 */
async function regenerateClipDirect() {
  const shot = activeShot.value
  if (!shot) return
  directRegenLoading.value = true
  try {
    // 只提交已绑定资产的槽位；用户手动关闭的槽位保留 enabled=false，避免被系统槽位重新补齐
    const references = workspaceBindings.value
      .filter((r) => r.asset_id)
      .map((r) => ({
        slot_id: r.slot_id,
        asset_id: r.asset_id,
        asset_name: r.asset_name ?? null,
        node_id: r.node_id ?? null,
        input_key: r.input_key ?? null,
        image_no: r.image_no ?? null,
        semantic: r.semantic ?? 'reference',
        enabled: r.enabled !== false,
      }))
    await submitRegenerateWorkspace(shot.id, {
      target: 'clip',
      workflow_id: clipWorkflowId.value,
      final_prompt: editVideoPrompt.value,
      references,
      idempotency_key: `clip-rerun-${shot.id}-${Date.now()}`,
    })
    message.success('已按当前参数提交重新生成，完成后将替换原视频')
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
    queryClient.invalidateQueries({ queryKey: ['jobs', projectId, 'generate_clip'] })
    void trackClipProgress()
    // 提交成功（无异常）后稍作停留让提示可见，再关闭镜头详情
    window.setTimeout(() => {
      drawerVisible.value = false
    }, 1000)
  } catch (e: unknown) {
    message.error(`重新生成失败: ${(e as Error)?.message ?? e}`)
  } finally {
    directRegenLoading.value = false
  }
}
/** 抽屉内点击视频按钮：未生成过直接提交；已生成/在生成中直接按当前提示词与参考图重新生成 */
function handleClipActionClick() {
  if (!activeShot.value) return
  if (!clipActionIsRegenerate.value) {
    handleGenerateFromDrawer('clip')
    return
  }
  void regenerateClipDirect()
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

// --- Drawer ---
const drawerVisible = ref(false)
const activeShot = ref<Shot | null>(null)

/** 系统级默认负向提示词：统一维护，用户未自定义时作为默认值；图文可共用，是否启用按模型能力决定 */
const DEFAULT_NEGATIVE_PROMPT = [
  'low quality, worst quality, low resolution, blurry, out of focus, pixelated, noisy',
  'compression artifacts, excessive motion blur, overexposed, underexposed, poor lighting',
  'unnatural colors, oversaturated, washed-out colors, excessive sharpening',
  'distorted image, visual artifacts, malformed anatomy, deformed body, unnatural body proportions',
  'deformed face, asymmetrical facial features, malformed hands, malformed feet',
  'extra fingers, missing fingers, extra limbs, missing limbs, duplicated body parts',
  'unnatural pose, unnatural movement, stiff motion, jerky motion, inconsistent motion',
  'temporal inconsistency, flickering, jitter, ghosting, double images, frame instability',
  'warped objects, distorted geometry, inconsistent perspective, inconsistent lighting, inconsistent shadows',
  'floating objects, duplicated objects, disappearing objects, unwanted objects',
  'unexpected background changes, inconsistent character appearance, identity drift',
  'inconsistent facial features, inconsistent clothing, inconsistent hairstyle, inconsistent object shapes',
  'unnatural physical interactions, broken contact between objects, clipping, penetration artifacts',
  'text artifacts, random text, watermark, logo, unwanted borders, unwanted black bars',
].join(', ')

const editVideoPrompt = ref('')
/** 负向提示词：默认用统一模板，用户可改；工作流未声明支持时不显示也不传参 */
const editNegativePrompt = ref(DEFAULT_NEGATIVE_PROMPT)
/** 当前视频工作流是否支持负向提示词，由重生成配置接口的 negative_prompt.supported 决定 */
const negativeSupported = ref(false)
/** 打开抽屉时的原始视频提示词：用于判断用户是否真的改过，避免无改动也触发下游产物失效 */
const originVideoPrompt = ref('')

/** 按 MiniMax H3 提示词规范（I2VA：首帧关键帧全参考）构建默认视频提示词。
 * <Picture 1> 为 H3 图片引用标签（对应后端自动注入的关键帧参考图 ref_image）；
 * @角色名/@场景名 为项目注入语法，后端据此把定妆图/基准图文件注入生成入参 __media__。 */
function buildH3VideoPrompt(shot: Shot): string {
  const scene = (scenesQuery.data.value?.items ?? []).find((s) => s.id === shot.scene_id)
  const ch = (charsData.value?.items ?? []).find((c) => c.id === shot.character_id)
  const refs: string[] = []
  if (ch) refs.push(`@${ch.name}`)
  if (scene) refs.push(`@${scene.title}`)
  const refText = refs.length ? `${refs.join(' ')} ` : ''
  const desc = shot.description || shot.dialogue || '（补充画面描述：主体、动作、环境、镜头运动）'
  return [
    'For the target video, at 0.00 seconds into the target video, <Picture 1> is fully referenced.',
    '',
    `integrated_multimodal_description: [Shot 1] Live-action, cinematic. ${refText}${desc} <Picture 1> defines the style, subjects and composition at 0.00 seconds; identity, clothing, colors and spatial relationships remain consistent throughout the ${shot.duration}-second video.`,
    'overall_soundscape: Ambient sound of the scene plus physical action sounds synchronized with the movement; non-verbal human sounds where appropriate.',
    'non_diegetic_music: (描述背景音乐情绪、节奏与乐器；无则填 None.)',
  ].join('\n')
}

function openDrawer(shot: Shot) {
  activeShot.value = shot
  void loadNegativeSupported(shot)
  void loadRefBindings(shot.id)
  editVideoPrompt.value = shot.video_prompt || buildH3VideoPrompt(shot)
  // 负向提示词无独立存储字段，每次打开从系统默认模板起，用户可在本次生成前自行改写
  editNegativePrompt.value = DEFAULT_NEGATIVE_PROMPT
  // 以打开抽屉时实际展示的内容作为比对基准：自动填充的默认视频提示词不算用户改动
  originVideoPrompt.value = editVideoPrompt.value
  drawerVisible.value = true
}

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
interface StatusInfo { label: string; color: string; icon?: string }
function getShotStatus(shot: Shot): StatusInfo {
  const ca = shot.current_assets
  // 正在生成优先：重新生成时镜头已有旧视频，也要显示"视频生成中"而不是"已完成"
  const progressJob = inflightClipByShot.value.get(shot.id)
  const jobStatus = progressJob?.status ?? activeClipJobByShot.value.get(shot.id)
  if (jobStatus === 'running' || jobStatus === 'submission_unknown') {
    const pct = progressJob?.progress ?? clipProgressByShot.value.get(shot.id)
    return {
      label: typeof pct === 'number' ? `视频生成中 ${pct}%` : '视频生成中',
      color: '#3B82F6',
    }
  }

  const has = ca && (ca.keyframe || ca.clip || ca.audio)
  if (!has) {
    return { label: '未生成', color: 'var(--text-tertiary)' }
  }
  // 已有视频：排队的"重新生成"不影响展示，仍以已有产物为准（排队信息保留在任务列表里）
  if (ca?.clip) {
    const con = shot.consistency
    if (con) {
      if (con.verdict === 'passed') return { label: '已完成', color: '#22C55E' }
      if (con.verdict === 'review') return { label: '待审核', color: '#F59E0B' }
      if (con.verdict === 'failed') return { label: '待重做', color: '#EAB308' }
    }
    return { label: '已完成', color: '#22C55E' }
  }

  // 还没有视频：显示排队/等前序（带队列名次），失败优先提示重试，否则待生成
  if (failedClipByShot.value.has(shot.id)) {
    return { label: '视频生成失败', color: '#EF4444' }
  }
  if (jobStatus === 'queued') {
    const rank = progressJob?.queue_rank
    return { label: rank ? `排队第 ${rank} 位` : '视频排队中', color: '#94A3B8' }
  }
  if (jobStatus === 'chained_wait') {
    const rank = progressJob?.queue_rank
    return { label: rank ? `等待前序（第 ${rank} 位）` : '等待前序镜头完成', color: '#A855F7' }
  }
  return { label: '待生成视频', color: 'var(--text-tertiary)' }
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

// 负向提示词支持检测：镜头已有产物时从生成配置接口获取
async function loadNegativeSupported(shot: Shot) {
  negativeSupported.value = false
  const hasOutput = Boolean(shot.current_assets?.clip || shot.current_assets?.keyframe)
  if (!hasOutput) return
  try {
    const cfg = await fetchRegenerateConfig(shot.id, 'clip')
    negativeSupported.value = Boolean(cfg.negative_prompt?.supported)
  } catch {
    negativeSupported.value = false
  }
}

// --- 保存提示词 ---
// 后端在提示词变化时会做失效传播：改分镜图提示词会让分镜图与视频一并过期、
// 分镜图确认状态回退；只改视频提示词仅影响视频。
// 因此这里只提交真正改过的字段，未改动直接返回，避免误触导致已有产物作废。
const savingPrompts = ref(false)
async function handleSavePrompts() {
  if (!activeShot.value) return
  const shot = activeShot.value

  const videoChanged = editVideoPrompt.value !== originVideoPrompt.value
  if (!videoChanged) {
    message.info('提示词没有改动')
    return
  }
  // 视频看板只维护视频提示词：分镜图提示词属于分镜图环节，这里不再提交
  const payload: { video_prompt?: string } = {}
  if (videoChanged) payload.video_prompt = editVideoPrompt.value

  const hasClip = Boolean(shot.current_assets?.clip)
  if (videoChanged && hasClip) {
    message.warning('视频提示词已修改，原有视频会被标记为过期，需要重新生成。')
  }

  savingPrompts.value = true
  try {
    await updateShot(shot.id, payload)
    if (payload.video_prompt !== undefined) {
      shot.video_prompt = payload.video_prompt
      originVideoPrompt.value = payload.video_prompt
    }
    const list = shotsMap.value[shot.scene_id]
    const target = list?.find((s) => s.id === shot.id)
    if (target && payload.video_prompt !== undefined) target.video_prompt = payload.video_prompt
    message.success('提示词已保存')
    if (hasClip) {
      queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
      await loadSceneShots(shot.scene_id, shot.episode)
      const refreshed = shotsMap.value[shot.scene_id]?.find((s) => s.id === shot.id)
      if (refreshed) activeShot.value = refreshed
    }
  } catch (e: any) {
    message.error(`保存失败: ${e?.message ?? e}`)
  } finally {
    savingPrompts.value = false
  }
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
        :color="clipProgress.data.value?.all_done ? 'var(--state-success, #22c55e)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle, #e5e7eb)"
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
              <div class="shot-card" @click="openDrawer(shot)">
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

    <!-- Drawer -->
    <n-modal
      v-model:show="drawerVisible"
      preset="card"
      title="镜头详情"
      closable
      :bordered="false"
      :style="{ width: 'min(1180px, 94vw)' }"
      :content-style="{ maxHeight: 'calc(85vh - 56px)', overflowY: 'auto' }"
    >
        <template v-if="activeShot">
        <div class="detail-grid">
          <!-- 成片优先：分镜看板以完成视频生成为目标，打开抽屉先看到视频 -->
          <div class="drawer-hero">
            <div class="hero-head">
              <span class="section-name">镜头视频</span>
              <n-tag size="small" :bordered="false" :color="{ color: getShotStatus(activeShot).color, textColor: '#fff' }">
                {{ getShotStatus(activeShot).label }}
              </n-tag>
            </div>
            <video
              v-if="activeShot.current_assets?.clip?.url"
              :src="activeShot.current_assets.clip.url"
              controls
              preload="metadata"
              class="preview-video"
            />
            <div v-else class="hero-empty">
              <div class="hero-empty-title">该镜头尚未生成视频</div>
              <div class="hero-empty-hint">
                {{ activeShot.keyframe_confirmed ? '分镜图已确认，可在下方「生成操作」中提交视频生成。' : '需先到分镜图环节确认关键帧，之后才能生成视频。' }}
              </div>
            </div>
          </div>

          <div class="detail-body">
          <!-- 分区一：镜头信息与提示词 -->
          <section class="drawer-section">
            <div class="section-head">
              <span class="section-index">1</span>
              <span class="section-name">镜头信息与提示词</span>
            </div>
            <div class="shot-brief">
              <n-tag size="small" :bordered="false">{{ shotKindLabels[activeShot.shot_kind] }}</n-tag>
              <span class="brief-item">第 {{ activeShot.episode }} 集 · 第 {{ shotGlobalSeq(activeShot) }} 镜（全片第 {{ shotGlobalSeq(activeShot) }} 镜）</span>
              <span class="brief-item">{{ activeShot.duration }} 秒</span>
              <n-tag size="small" :bordered="false" :color="{ color: getShotStatus(activeShot).color, textColor: '#fff' }">
                {{ getShotStatus(activeShot).label }}
              </n-tag>
            </div>

            <div class="field">
              <div class="field-head">
                <label class="field-label">视频提示词（MiniMax H3）</label>
                <span class="field-badge">生视频</span>
              </div>
              <n-input v-model:value="editVideoPrompt" type="textarea" :rows="8" />
              <div class="field-hint">
                首帧指令 + integrated_multimodal_description / overall_soundscape / non_diegetic_music；串联生成时 &lt;Picture 1&gt; 为上一镜尾帧（本镜首帧），&lt;Picture 2&gt; 为本镜分镜图；@角色名/@场景名 自动携带定妆图与基准图。
              </div>
            </div>

            <div v-if="negativeSupported" class="field">
              <div class="field-head">
                <label class="field-label">负向提示词</label>
                <span class="field-badge soft">默认模板</span>
                <n-button
                  text
                  size="tiny"
                  @click="editNegativePrompt = DEFAULT_NEGATIVE_PROMPT"
                >恢复默认</n-button>
              </div>
              <n-input v-model:value="editNegativePrompt" type="textarea" :rows="4" />
              <div class="field-hint">
                仅在当前工作流支持负向提示词时提交，可自行改写。
              </div>
            </div>

            <div class="section-foot">
              <span class="foot-hint">负向提示词不单独保存，随本次生成提交。</span>
              <n-button size="small" :loading="savingPrompts" @click="handleSavePrompts">保存提示词</n-button>
            </div>
          </section>

          <!-- 分区二：参考图绑定管理 -->
          <section class="drawer-section">
            <div class="section-head">
              <span class="section-index">2</span>
              <span class="section-name">参考图</span>
              <n-button size="tiny" :loading="refBindingsSaving" @click="saveRefBindings" style="margin-left:auto">保存绑定</n-button>
            </div>

            <n-spin :show="refBindingsLoading">
              <table v-if="workspaceBindings.length" class="ref-binding-table">
                <thead>
                  <tr>
                    <th style="width:56px">预览</th>
                    <th style="min-width:90px">槽位</th>
                    <th>节点</th>
                    <th style="min-width:160px">绑定资产</th>
                    <th>启用</th>
                    <th style="width:48px"></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="ref in workspaceBindings" :key="ref.reference_id">
                    <td>
                      <img
                        v-if="ref.asset_id"
                        class="wb-thumb"
                        :src="`/api/assets/${ref.asset_id}/file`"
                        :alt="ref.asset_name ?? ref.slot_id"
                      />
                      <span v-else class="wb-mono">—</span>
                    </td>
                    <td>
                      <span class="wb-slot">{{ ref.slot_id }}</span>
                      <n-tag v-if="ref.required" type="warning" size="tiny">必需</n-tag>
                      <span v-if="ref.image_no != null" class="wb-imgno">Img{{ ref.image_no }}</span>
                    </td>
                    <td class="wb-mono">{{ ref.node_id ?? '—' }}</td>
                    <td>
                      <n-select
                        :value="ref.asset_id"
                        @update:value="(v) => ref.asset_id = v"
                        :options="shotAssetOptions"
                        size="tiny"
                        filterable
                        clearable
                        placeholder="选择资产"
                      />
                    </td>
                    <td>
                      <n-switch :value="ref.enabled" @update:value="(v) => ref.enabled = v" size="small" />
                    </td>
                    <td>
                      <n-button text type="error" size="tiny" @click="removeRefBinding(ref.reference_id)">移除</n-button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <div v-else class="panel-hint">
                暂无参考图绑定，请在下方添加或从工作台管理。
              </div>

              <div v-if="unboundRefSlots.length" class="wb-add-row" style="margin-top:8px">
                <span class="wb-hint-line">未绑定槽位：</span>
                <n-button
                  v-for="s in unboundRefSlots"
                  :key="s.param"
                  size="tiny"
                  secondary
                  @click="addRefBinding(s.param)"
                >
                  + {{ s.param }}（Img {{ s.image_no ?? '—' }}）
                </n-button>
              </div>
              <div v-if="chainFrameStatus === 'ready'" class="panel-hint" style="margin-top:6px">
                串联生成时本镜首帧（&lt;Picture 1&gt;）取上一镜实际尾帧，见上方 ref_prev_frame 绑定。
              </div>
              <div v-else-if="chainFrameStatus === 'missing'" class="panel-hint" style="margin-top:6px">
                串联生成时本镜首帧取上一镜实际尾帧；上一镜尚未生成视频尾帧，请先生成上一镜视频。
              </div>
            </n-spin>
          </section>

          <!-- 分区三：生成操作 -->
          <section class="drawer-section">
            <div class="section-head">
              <span class="section-index">3</span>
              <span class="section-name">生成操作</span>
            </div>
            <div v-if="!readiness.all" class="gate-hint">
              公共资产尚未齐全（角色定妆图 / 场景基准图 / 道具锚定图），请先到资产库补齐后再生成。
            </div>
            <!-- 视频生成档位：极速 5s / 标准 5s / 质量 10s -->
            <div class="tier-row">
              <span class="tier-label">视频档位</span>
              <n-select
                v-model:value="clipTier"
                :options="clipTierOptions"
                size="small"
                style="width: 210px"
              />
              <span class="tier-hint">极速/标准 5s、质量 10s 分别对应一套 4 步工作流</span>
            </div>
            <!-- 按当前阶段只展示可行的生成动作：未确认分镜图先生图，确认后再出视频 -->
            <div class="drawer-actions">
              <n-tooltip v-if="!activeShot.keyframe_confirmed">
                <template #trigger>
                  <n-button
                    type="primary"
                    :disabled="!readiness.all"
                    :loading="generateMutation.isPending.value"
                    @click="handleGenerateFromDrawer('keyframe')"
                  >
                    生成分镜图
                  </n-button>
                </template>
                {{ readiness.all ? '提交分镜图（关键帧）生成任务' : '公共资产尚未齐全，暂不能生成分镜图' }}
              </n-tooltip>
              <n-tooltip v-else>
                <template #trigger>
                  <n-button
                    type="primary"
                    :disabled="!readiness.all"
                    :loading="generateMutation.isPending.value || directRegenLoading"
                    @click="handleClipActionClick"
                  >
                    {{ clipActionLabel }}
                  </n-button>
                </template>
                {{ readiness.all
                  ? (clipActionIsRegenerate ? '重新生成视频：使用当前修改后的 H3 提示词、参考图与视频档位（5s/10s）重新生成，完成后替换原视频' : '提交视频生成任务')
                  : '公共资产尚未齐全，暂不能生成视频' }}
              </n-tooltip>
            </div>
            <div class="action-hint">
              <template v-if="!activeShot.keyframe_confirmed">
                当前阶段：待确认分镜图
              </template>
              <template v-else>
                当前阶段：分镜图已确认
              </template>
            </div>
          </section>
          <!-- 分区四：音频（音频模块改造：镜头详情音频面板） -->
          <section class="drawer-section">
            <div class="section-head">
              <span class="section-index">4</span>
              <span class="section-name">音频</span>
            </div>
            <ShotAudioPanel
              :project-id="projectId"
              :shot-id="activeShot.id"
              :episode="activeShot.episode"
            />
          </section>
          </div>
        </div>
        </template>
    </n-modal>
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
          <span style="font-size: 13px; color: #666">视频档位</span>
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
              <div style="font-size: 12px; color: #999; margin-top: 2px">
                各镜头互不依赖，不使用上一镜尾帧，多 ComfyUI 实例可并行生成，速度快
              </div>
            </n-radio>
            <n-radio value="chained">
              链式生成（串行）
              <div style="font-size: 12px; color: #999; margin-top: 2px">
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
  padding: 16px;
  min-height: calc(100vh - var(--app-header-height, 56px));
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
.episode-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.episode-label { font-size: 13px; color: var(--text-secondary); }
.episode-chip { border: 1px solid var(--border-subtle, #e5e7eb); background: var(--bg-panel, #fff); border-radius: 999px; padding: 6px 16px; font-size: 14px; color: var(--text-secondary); cursor: pointer; transition: all 0.15s ease; }
.episode-chip:hover { border-color: var(--brand-primary); color: var(--brand-primary); }
.episode-chip.active { background: rgba(53, 179, 166, 0.1); border-color: var(--brand-primary); color: var(--brand-primary); font-weight: 600; }
.gate-banner { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; background: var(--bg-panel, #fff); }
.gate-banner.ok { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.06); }

/* ===== 视频生成总体进度面板 ===== */
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
.gen-state.running { color: var(--brand-primary); }
.gen-state.ok { color: #15803d; font-weight: 500; }
.gen-state.fail { color: #b91c1c; }
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
.gen-inflight-state.running { color: var(--brand-primary); }
.gen-inflight-state.chained_wait { color: var(--accent-amber); }
.gen-inflight-more { font-size: 11px; color: var(--text-tertiary); align-self: center; }
.gate-badge { font-size: 14px; }
.gate-text { font-size: 13px; color: var(--text-primary, #333); margin-right: auto; }
.page-loading {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}
.scenes-container {
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.scene-group {
  background: transparent;
}
.scene-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
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
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 8px;
}
.shot-card-wrapper {
  position: relative;
  flex-shrink: 0;
}
.shot-card {
  width: 220px;
  height: 320px;
  background: var(--bg-panel);
  border-radius: 12px;
  overflow: hidden;
  cursor: pointer;
  border: 1px solid var(--border-subtle);
  transition: border-color 0.2s;
  position: relative;
}
.shot-check {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 2;
  background: rgba(255, 255, 255, 0.75);
  border-radius: 4px;
  padding: 2px;
}
.shot-card:hover {
  border-color: var(--brand-primary);
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
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-on-brand, #fff);
  white-space: nowrap;
}
.shot-info {
  padding: 8px 12px;
}
.shot-title {
  font-size: 13px;
  color: var(--text-primary);
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.shot-meta {
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.drawer-preview {
  margin-bottom: 16px;
}
.preview-img {
  width: 100%;
  border-radius: 8px;
}
.preview-img :deep(img) {
  width: 100%;
  max-height: 320px;
  object-fit: contain;
  display: block;
}
.drawer-video {
  margin-bottom: 16px;
}
.drawer-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}
.preview-video {
  width: 100%;
  border-radius: 8px;
  background: #000;
  display: block;
}
.preview-placeholder {
  width: 100%;
  height: 200px;
  background: var(--bg-elevated);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-tertiary);
}
.field-label {
  display: block;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}
:deep(.n-input__textarea-el) {
  font-size: 13px;
  line-height: 1.6;
}
.drawer-actions {
  display: flex;
  gap: 8px;
}
.tier-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.tier-label {
  font-size: 13px;
  color: var(--text-secondary, #666);
}
.tier-hint {
  font-size: 12px;
  color: var(--text-tertiary, #999);
}

/* ===== 镜头详情抽屉分区 ===== */
.drawer-hero {
  margin-bottom: 4px;
}
.hero-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.hero-empty {
  padding: 28px 16px;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  border-radius: 10px;
  text-align: center;
  background: var(--bg-elevated, #fafafa);
}
.hero-empty-title {
  font-size: 13px;
  color: var(--text-secondary, #666);
  margin-bottom: 6px;
}
.hero-empty-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary, #909399);
}
.ref-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ref-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px;
  border: 1px solid var(--border-subtle, #e5e7eb);
  border-radius: 8px;
  background: var(--bg-panel, #fff);
}
.ref-item-meta {
  flex: 1;
  min-width: 0;
}
.ref-item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary, #333);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ref-item-role {
  margin-top: 3px;
  font-size: 11px;
  color: var(--text-tertiary, #909399);
}
.ref-item-req {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(245, 158, 11, 0.12);
  color: #b45309;
}
.ref-item-slot {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-elevated, #f5f7fa);
  font-family: monospace;
  font-size: 10px;
}
.ref-thumb-empty {
  width: 56px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  border-radius: 6px;
  font-size: 11px;
  color: var(--text-tertiary, #909399);
  flex-shrink: 0;
}
.ref-tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 400;
}
.ref-tag.ok {
  background: rgba(34, 197, 94, 0.12);
  color: #15803d;
}
.ref-tag.plan {
  background: rgba(53, 179, 166, 0.12);
  color: #4338ca;
}
.ref-tag.warn {
  background: rgba(245, 158, 11, 0.14);
  color: #b45309;
}
.ref-item-input {
  margin-top: 2px;
  font-size: 10px;
  color: var(--text-tertiary, #909399);
  font-family: monospace;
}
.ref-source-hint {
  margin-top: 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary, #909399);
}
.ref-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-tertiary, #909399);
}
.drawer-section {
  padding: 14px 0;
  border-bottom: 1px solid var(--divider-color, #efeff5);
}
.drawer-section:last-of-type {
  border-bottom: none;
  padding-bottom: 4px;
}
.section-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.section-index {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--brand-primary);
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  flex-shrink: 0;
}
.section-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary, #333);
}
.shot-brief {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.brief-item {
  font-size: 12px;
  color: var(--text-secondary, #666);
}
.field {
  margin-bottom: 12px;
}
.field:last-child {
  margin-bottom: 0;
}
.field-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.field-badge {
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(53, 179, 166, 0.12);
  color: #4338ca;
  font-size: 10px;
  line-height: 16px;
}
.field-badge.soft {
  background: var(--bg-elevated, #f5f7fa);
  color: var(--text-tertiary, #909399);
}
.field-head :deep(.n-button) {
  margin-left: auto;
}
.field-hint {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary, #909399);
}
.panel-hint {
  padding: 12px;
  border: 1px dashed var(--border-subtle, #e5e7eb);
  border-radius: 8px;
  font-size: 12px;
  color: var(--text-tertiary, #909399);
}
.gate-hint {
  margin-bottom: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(245, 158, 11, 0.1);
  font-size: 12px;
  color: #b45309;
}
.section-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 12px;
}
.foot-hint {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary, #909399);
}
.action-hint {
  margin-top: 8px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary, #909399);
}
@media (max-width: 768px) {
  .drawer-section { padding: 12px 0; }
  .section-name { font-size: 13px; }
  .drawer-actions { flex-wrap: wrap; }
  .drawer-actions :deep(.n-button) { flex: 1; }
  .section-foot { flex-wrap: wrap; }
  .foot-hint { flex: 1 1 100%; }
  .ref-item { gap: 10px; padding: 8px 6px; }
  .ref-item-name { white-space: normal; }
}
@media (max-width: 480px) {
  .drawer-section { padding: 10px 0; }
  .field-hint, .ref-source-hint { font-size: 11px; }
  .ref-item-input, .ref-item-slot { font-size: 10px; }
  .drawer-actions :deep(.n-button) { width: 100%; }
}

/* ===== 窄屏适配 ===== */
@media (max-width: 768px) {
  .storyboard-page { padding: 12px; }
  .scenes-container { gap: 16px; }
  .shots-row {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    overflow-x: visible;
  }
  .shot-card-wrapper { width: 100%; }
  .shot-card { width: 100%; height: auto; }
  .shot-thumb { height: 150px; }
  .shot-info { padding: 6px 8px; }
}
@media (max-width: 480px) {
  .shots-row { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); }
}

/* 居中大弹窗：左视频右分区 */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(320px, 460px) minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.detail-body { min-width: 0; }
.detail-body .drawer-section:first-of-type { padding-top: 0; }
@media (max-width: 1020px) {
  .detail-grid { grid-template-columns: 1fr; gap: 16px; }
}

/* 弹窗标题接入页面衬线标题体系 */
:deep(.n-card-header__main) {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.01em;
}

/* 宽屏两栏时左栏吸附，右栏长内容滚动时画面保持可见 */
@media (min-width: 981px) {
  .detail-grid .drawer-hero {
    position: sticky;
    top: 0;
  }
}

/* 参考图绑定管理表格 */
.ref-binding-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.ref-binding-table th,
.ref-binding-table td {
  border-bottom: 1px solid var(--border-color, #e8e8e8);
  padding: 6px 8px;
  text-align: left;
  vertical-align: middle;
}
.ref-binding-table th {
  background: var(--bg-elevated, #fafafa);
  font-weight: 600;
  color: #666;
}
.ref-binding-table .wb-slot {
  font-family: monospace;
  font-size: 12px;
}
.ref-binding-table .wb-mono {
  font-family: monospace;
  color: #888;
  font-size: 11px;
}
.ref-binding-table .wb-imgno {
  display: inline-block;
  margin-left: 4px;
  padding: 0 4px;
  background: #f0f0f0;
  border-radius: 3px;
  font-size: 10px;
  color: #666;
}
.ref-binding-table .wb-thumb {
  width: 44px;
  height: 28px;
  object-fit: cover;
  border-radius: 3px;
  background: #f0f0f0;
  display: block;
}
.wb-add-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.wb-hint-line {
  font-size: 12px;
  color: #999;
  margin: 4px 0 8px;
}
</style>
