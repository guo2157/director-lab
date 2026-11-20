<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NIcon, NProgress, useMessage } from 'naive-ui'
import { ArrowForwardOutline } from '@vicons/ionicons5'
import { listScenes, listSceneShots, confirmShotKeyframe, unconfirmShotKeyframe } from '@/api/script'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { lockAsset, unlockAsset, replaceAsset } from '@/api/assets'
import { getProject } from '@/api/projects'
import { getKeyframeGenerationProgress } from '@/api/generation'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
import type { Shot } from '@/types/script'
import type { KeyframeGenerationJob, KeyframeGenerationProgress } from '@/types/generation'
import type { GenerateShotPayload } from '@/types/job'
import StageCard from '@/components/ui/StageCard.vue'
import StatusTag from '@/components/ui/StatusTag.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import ShotCard from '@/components/storyboard-images/ShotCard.vue'
import BatchToolbar from '@/components/storyboard-images/BatchToolbar.vue'
import ShotDetailDialog from '@/components/storyboard-images/ShotDetailDialog.vue'

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

/** 顶部进度条数值：已出图 / 全部镜头 */
const progressPercent = computed(() => {
  const total = allShots.value.length
  if (!total) return 0
  return Math.round((keyframeShots.value.length / total) * 100)
})

/** 顶部进度数字：已出图 / 全部镜头（百分比）。状态句已删除——
 *  「正在生成 N 张」「已全部生成完成」这类叙述与数字、进度条、卡片状态标签重复。 */
const progressNums = computed(
  () => `${keyframeShots.value.length}/${allShots.value.length}（${progressPercent.value}%）`,
)

/** 审核标签只在未通过时出现：通过后它是已完成的事，标签本身没有信息量 */
const reviewTag = computed(() => {
  if (reviewRejected.value) return { kind: 'failed' as const, label: '审核已驳回' }
  return { kind: 'pending' as const, label: '待审核' }
})

/** 仅在审核未通过时渲染（模板有 v-if 守卫），因此不再需要「已通过」分支 */
const gateText = computed(() => {
  if (reviewRejected.value) return '公共资产审核被驳回：请返回资产库修改或替换资产后重新提交审核'
  return '公共资产尚未通过人工审核：请先在资产库完成审核，才能生成分镜图'
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
  const jobs = (scenesQuery.data.value?.items ?? []).map((sc) => loadSceneShots(sc.id, sc.episodes?.[0] ?? 1))
  return Promise.all(jobs)
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
    const planIds = (res.items || []).filter((p) => p.owner_id && pickedIds.has(p.owner_id)).map((p) => p.id)
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

// --- 网格分组 ---
/** 场景分组：组内按全片镜号顺序。状态筛选 chips 已删除，
 *  所有镜头始终全量展示——进度卡的数字与卡片自身的状态标签已足够定位。 */
const sceneGroups = computed(() =>
  [...(scenesQuery.data.value?.items ?? [])]
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
    .map((scene) => ({
      scene,
      episode: scene.episodes?.[0] ?? 1,
      shots: [...(shotsMap.value[scene.id] ?? [])].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)),
    })),
)

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
      <span class="gen-nums">{{ progressNums }}</span>
      <n-progress
        class="gen-bar"
        type="line"
        :percentage="progressPercent"
        :height="6"
        :border-radius="3"
        :show-indicator="false"
        :color="allShots.length > 0 && keyframeShots.length === allShots.length ? 'var(--state-success)' : 'var(--brand-primary)'"
        rail-color="var(--border-subtle)"
      />
      <StatusTag v-if="!reviewApproved" :status="reviewTag.kind" :label="reviewTag.label" />
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

      <template #meta>
        <!-- 正在生成的镜头明细：场景 + 镜号 + 百分比 -->
        <div v-if="keyframeProgress.data.value?.inflight?.length" class="gen-inflight">
          <span v-for="job in keyframeProgress.data.value.inflight" :key="job.job_id" class="gen-inflight-item">
            <span class="gen-inflight-name">{{ job.scene_title || '未知场景' }} · 镜 {{ job.shot_seq ?? '—' }}</span>
            <span class="gen-inflight-state" :class="job.status">
              {{ job.status === 'running' ? (job.progress != null ? `生成中 ${job.progress}%` : '生成中') : job.status === 'queued' ? '排队中' : '提交确认中' }}
            </span>
          </span>
        </div>
        <!-- 审核未通过时，阻断原因放在进度卡第二行：它与「做到哪了」同属状态陈述 -->
        <span v-if="!reviewApproved" class="gate-text">{{ gateText }}</span>
      </template>

      <template #actions>
        <n-button v-if="!reviewApproved" size="small" quaternary @click="router.push(`/project/${projectId}/assets`)">
          去资产库
        </n-button>
        <n-button size="small" quaternary :loading="keyframeProgress.loading.value" @click="keyframeProgress.refresh()">
          刷新进度
        </n-button>
        <n-button size="small" quaternary @click="refreshAll">刷新镜头</n-button>
        <n-button
          size="small"
          type="primary"
          :disabled="!allConfirmed"
          :title="allConfirmed ? '进入分镜看板生成视频' : '需先逐镜确认全部分镜图'"
          @click="router.push(`/project/${projectId}/storyboard`)"
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
      :can-generate="reviewApproved"
      :batch-loading="batchMutation.isPending.value"
      :confirm-loading="batchConfirming"
      :inflight-count="keyframeProgress.data.value?.inflight_count ?? 0"
      :workflow-id="generateWorkflowId"
      @update:workflow-id="(v: string | undefined) => (generateWorkflowId = v)"
      @toggle-all="toggleSelectAll"
      @generate="handleBatchGenerate"
      @confirm="handleBatchConfirm"
    />

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
            :can-generate="reviewApproved"
            :prompt="promptDraft[shot.id] ?? shot.image_prompt ?? ''"
            @select="(v) => toggleSelect(shot, v)"
            @open="openDetail(shot)"
            @generate="handleGenerate(shot)"
            @confirm="handleConfirm(shot, g.scene.id, g.episode)"
            @unconfirm="handleUnconfirm(shot, g.scene.id, g.episode)"
            @lock="handleToggleLock(shot)"
            @replace="pickReplace(shot)"
          />
        </div>
      </section>
    </div>

    <!-- 镜头详情：大图 + 参考图 + 重新生成工作台 -->
    <ShotDetailDialog
      ref="dialogRef"
      :project-id="projectId"
      :review-approved="reviewApproved"
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

/* ===== 执行进度卡内容（容器样式由 StageCard 提供） ===== */
.gen-bar {
  flex: 1;
  min-width: 120px;
}
.gen-nums {
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
}
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
.gen-inflight {
  display: flex;
  gap: var(--sp-1);
  flex-wrap: wrap;
}
.gen-inflight-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px var(--sp-2);
  font-size: 11px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.gen-inflight-name {
  color: var(--text-primary);
}
.gen-inflight-state {
  color: var(--text-secondary);
}
.gen-inflight-state.running {
  color: var(--brand-primary);
}
.gen-inflight-state.queued {
  color: var(--accent-amber);
}

/* 审核未通过时的阻断说明：位于进度卡第二行，不再是独立横幅 */
.gate-text {
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger-text, var(--state-danger));
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
  .shots-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.hidden-file {
  display: none;
}
</style>
