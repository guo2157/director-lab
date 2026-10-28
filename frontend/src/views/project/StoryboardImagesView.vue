<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NSpace, NSpin, NEmpty, NTag, NTooltip, NInput, NCheckbox, NImage, useMessage } from 'naive-ui'
import { listScenes, listSceneShots, confirmShotKeyframe, unconfirmShotKeyframe } from '@/api/script'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import { lockAsset, unlockAsset, replaceAsset } from '@/api/assets'
import { getProject } from '@/api/projects'
import type { Shot } from '@/types/script'
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
const keyframeShots = computed(() => allShots.value.filter((s) => !!s.current_assets?.keyframe))
const confirmedShots = computed(() => allShots.value.filter((s) => s.keyframe_confirmed))
const allConfirmed = computed(
  () => allShots.value.length > 0 && allShots.value.every((s) => !!s.current_assets?.keyframe && s.keyframe_confirmed),
)

const generateMutation = useMutation({
  mutationFn: (payload: { shotId: string; params?: Record<string, string> }) =>
    generateShot(payload.shotId, { target: 'keyframe', params: payload.params } as GenerateShotPayload),
  onSuccess: () => message.success('分镜图生成任务已提交，完成后请刷新查看'),
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
  onSuccess: (res: any) => message.success(`已提交 ${res?.count ?? 0} 个分镜图任务，完成后请刷新查看`),
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
    message.success(`已按定义提交 ${gen.count} 个分镜图任务，完成后请刷新查看`)
    if (gen.skipped?.length) {
      const reasons = [...new Set(gen.skipped.map((s) => s.reason))].join('、')
      message.info(`另有 ${gen.skipped.length} 条定义被跳过：${reasons}`)
    }
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
      <n-button size="small" type="primary" :disabled="!allConfirmed" @click="router.push(`/project/${projectId}/storyboard`)">
        去分镜看板
      </n-button>
    </div>

    <input ref="fileInputRef" type="file" accept="image/*" class="hidden-file" @change="onReplaceFileChange" />

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
        size="small"
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
            <div v-for="shot in shotsMap[scene.id] || []" :key="shot.id" class="shot-card">
              <div class="shot-thumb">
                <div class="thumb-overlay">
                  <n-checkbox :checked="!!selectedIds[shot.id]" @update:checked="toggleSelect(shot)" />
                  <n-tag v-if="shot.current_assets?.keyframe?.locked" size="tiny" type="warning">已锁定</n-tag>
                </div>
                <n-image
                  v-if="shot.current_assets?.keyframe?.thumb_url || shot.current_assets?.keyframe?.url"
                  class="shot-thumb-img"
                  :src="shot.current_assets?.keyframe?.thumb_url || shot.current_assets?.keyframe?.url || ''"
                  :preview-src="shot.current_assets?.keyframe?.url || shot.current_assets?.keyframe?.thumb_url || ''"
                  object-fit="cover"
                  alt="分镜图"
                />
                <div v-else class="thumb-empty">未生成分镜图</div>
              </div>
              <div class="shot-body">
                <div class="shot-head">
                  <span class="shot-name">{{ shot.title || `镜头 ${shot.seq}` }}</span>
                  <span
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
                <n-space :size="8" class="shot-actions">
                  <n-tooltip>
                    <template #trigger>
                      <n-button
                        size="small"
                        type="primary"
                        :disabled="!reviewApproved || !!shot.current_assets?.keyframe?.locked"
                        :loading="generateMutation.isPending.value"
                        @click="handleGenerate(shot)"
                      >{{ shot.current_assets?.keyframe ? '重新生成' : '生成分镜图' }}</n-button>
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
.summary-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.page-loading { display: block; margin: 48px auto; }
.state-block { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 48px 24px; }
.state-text { margin: 0; color: var(--text-secondary); font-size: 14px; }
.scene-group { margin-bottom: 20px; }
.scene-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.scene-seq { color: var(--text-tertiary); font-size: 13px; }
.scene-title { font-weight: 600; font-size: 14px; }
.shots-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
.shot-card { display: flex; flex-direction: column; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: var(--radius, 10px); overflow: hidden; background: var(--bg-panel, #fff); }
.shot-thumb { aspect-ratio: 16 / 9; height: auto; background: var(--bg-elevated, #f5f5f5); display: flex; align-items: center; justify-content: center; }
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
.thumb-overlay { position: absolute; top: 6px; left: 6px; right: 6px; display: flex; align-items: center; justify-content: space-between; z-index: 2; }
@media (max-width: 768px) {
  .gate-banner { flex-wrap: wrap; }
  .scene-header { flex-wrap: wrap; }
  .shot-actions { flex-wrap: wrap; }
  .shots-grid { grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); }
}
@media (max-width: 480px) {
  .shots-grid { grid-template-columns: 1fr; }
}
</style>
