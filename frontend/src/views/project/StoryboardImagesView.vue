<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NIcon, NProgress, useMessage } from 'naive-ui'
import { ArrowForwardOutline } from '@vicons/ionicons5'
import { listScenes, listSceneShots } from '@/api/script'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { replaceAsset } from '@/api/assets'
import { getProject } from '@/api/projects'
import { getKeyframeGenerationProgress } from '@/api/generation'
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

/** 顶部进度数字：已出图 / 全部镜头（百分比）。状态句已删除——
 *  「正在生成 N 张」「已全部生成完成」这类叙述与数字、进度条、卡片状态标签重复。 */
const progressNums = computed(
  () => `${keyframeShots.value.length}/${allShots.value.length}（${progressPercent.value}%）`,
)

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
  const picked = selectedList.value
  if (!picked.length) {
    message.warning('请先勾选需要生成分镜图的镜头')
    return
  }
  // 不再有前端审核前置与锁定检查：生成条件由后端 assets_gate 权威校验
  // （必需资产是否真实生成完成），前端重复判断只会造成两套口径。
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
      </template>

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
      :workflow-id="generateWorkflowId"
      @update:workflow-id="(v: string | undefined) => (generateWorkflowId = v)"
      @toggle-all="toggleSelectAll"
      @generate="handleBatchGenerate"
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
