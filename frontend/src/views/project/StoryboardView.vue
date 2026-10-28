<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpace, NSpin, NEmpty, NTag, NDrawer, NDrawerContent,
  NInput, NTooltip, NCheckbox, NImage, useMessage,
} from 'naive-ui'
import { listCharacters, listProps, listScenes, listSceneShots, reorderSceneShots } from '@/api/script'
import { generateShot, generateProjectShots } from '@/api/storyboard'
import { listAssetPlans, generateFromPlans } from '@/api/assetPlans'
import RegenerateConfigPanel from '@/components/RegenerateConfigPanel.vue'
import type { Scene, Shot, ShotKind } from '@/types/script'
import type { GenerateShotPayload } from '@/types/job'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

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
    message.success('生成任务已提交')
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  },
  onError: (e: any) => {
    message.error(`生成失败: ${e?.message ?? e}`)
  },
})

/** 抽屉内生成：携带编辑后的提示词（后端 params.prompt 覆盖默认值，@角色/@场景 自动注入参考图） */
function handleGenerateFromDrawer(target: 'keyframe' | 'clip') {
  if (!activeShot.value) return
  const params: Record<string, string> = {}
  if (target === 'keyframe') {
    if (editImagePrompt.value) params.prompt = editImagePrompt.value
    if (editNegativePrompt.value) params.negative_prompt = editNegativePrompt.value
  } else {
    if (editVideoPrompt.value) params.prompt = editVideoPrompt.value
  }
  generateMutation.mutate({
    shotId: activeShot.value.id,
    payload: { target, params } as GenerateShotPayload,
  })
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
const editImagePrompt = ref('')
const editVideoPrompt = ref('')
const editNegativePrompt = ref('')

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
  editImagePrompt.value = shot.image_prompt ?? ''
  editVideoPrompt.value = shot.video_prompt || buildH3VideoPrompt(shot)
  editNegativePrompt.value = ''
  drawerVisible.value = true
}

/** 重新生成目标：已生成分镜视频时配置面板展示视频配置，否则展示分镜图配置。
 * 重新生成必须基于已存在的产物版本，无产物时面板不展示，引导先完成首次生成。 */
const regenerateTarget = computed<'keyframe' | 'clip' | null>(() => {
  const assets = activeShot.value?.current_assets
  if (!assets) return null
  if (assets.clip) return 'clip'
  if (assets.keyframe) return 'keyframe'
  return null
})

// --- Status badge ---
interface StatusInfo { label: string; color: string; icon?: string }
function getShotStatus(shot: Shot): StatusInfo {
  const ca = shot.current_assets
  const has = ca && (ca.keyframe || ca.clip || ca.audio)
  if (!has) {
    return { label: '未生成', color: 'var(--text-tertiary)' }
  }
  const con = shot.consistency
  if (con) {
    if (con.verdict === 'passed') return { label: '已完成', color: '#22C55E' }
    if (con.verdict === 'review') return { label: '待审核', color: '#F59E0B' }
    if (con.verdict === 'failed') return { label: '待重做', color: '#EAB308' }
  }
  return { label: '已完成', color: '#22C55E' }
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
  mutationFn: (ids: string[]) => generateProjectShots(projectId.value, { shot_ids: ids, target: 'clip' }),
  onSuccess: (res: any) => {
    message.success(`已提交 ${res?.count ?? 0} 个分镜视频任务，完成后请刷新查看`)
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  },
  onError: (e: any) => message.error(`批量出视频失败: ${e?.message ?? e}`),
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
  void submitClipsByPlans(confirmed)
}

/** 按已定稿的视频定义批量提交：名称与关联以库中定义为准；定义缺失时回退镜头级批量 */
async function submitClipsByPlans(picked: Shot[]) {
  try {
    const res = await listAssetPlans(projectId.value, { plan_type: 'clip' })
    const pickedIds = new Set(picked.map((s) => s.id))
    const planIds = (res.items || [])
      .filter((p) => p.owner_id && pickedIds.has(p.owner_id))
      .map((p) => p.id)
    if (!planIds.length) {
      batchClipMutation.mutate(picked.map((s) => s.id))
      return
    }
    const gen = await generateFromPlans(projectId.value, { plan_type: 'clip', plan_ids: planIds })
    message.success(`已按定义提交 ${gen.count} 个分镜视频任务，完成后请刷新查看`)
    const skipped = (gen.skipped || []).filter((s) => s.reason !== 'no_pending_plans')
    if (skipped.length) {
      const reasons = [...new Set(skipped.map((s) => s.reason))].join('、')
      message.info(`另有 ${skipped.length} 条定义被跳过：${reasons}`)
    }
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  } catch (e: any) {
    message.error(`批量出视频失败: ${e?.message ?? e}`)
  }
}
</script>

<template>
  <div class="storyboard-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">分镜看板</h1>
        <span class="page-hint">逐镜确认分镜图并生成镜头视频，全部完成后进入合成</span>
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
                  <div class="shot-title">{{ shot.title || `镜头 ${shot.seq}` }}</div>
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
    <n-drawer v-model:show="drawerVisible" :width="'min(480px, 100vw)'">
      <n-drawer-content title="镜头详情" closable>
        <template v-if="activeShot">
          <div class="drawer-preview">
            <n-image
              v-if="activeShot.current_assets?.keyframe?.url"
              :src="activeShot.current_assets.keyframe!.url"
              object-fit="contain"
              class="preview-img"
            />
            <div v-else class="preview-placeholder">暂无预览图</div>
          </div>
          <div v-if="activeShot.current_assets?.clip?.url" class="drawer-video">
            <div class="drawer-section-title">分镜视频</div>
            <video
              :src="activeShot.current_assets.clip.url"
              controls
              preload="metadata"
              class="preview-video"
            />
          </div>

          <n-space vertical :size="16">
            <div>
              <label class="field-label">提示词</label>
              <n-input v-model:value="editImagePrompt" type="textarea" :rows="4" />
            </div>
            <div>
              <label class="field-label">负向提示词</label>
              <n-input v-model:value="editNegativePrompt" type="textarea" :rows="2" />
            </div>
            <div>
              <label class="field-label">视频提示词（MiniMax H3 规范：首帧指令 + integrated_multimodal_description / overall_soundscape / non_diegetic_music；&lt;Picture 1&gt; 对应关键帧参考图，@角色名/@场景名 自动携带定妆图/基准图）</label>
              <n-input v-model:value="editVideoPrompt" type="textarea" :rows="8" />
            </div>
            <div class="drawer-actions">
              <n-button
                quaternary
                :disabled="!readiness.all"
                @click="router.push(`/project/${projectId}/storyboard-images`)"
              >
                去分镜图生成 / 确认
              </n-button>
              <n-tooltip>
                <template #trigger>
                  <n-button
                    type="primary"
                    :disabled="!readiness.all || !activeShot.keyframe_confirmed"
                    :loading="generateMutation.isPending.value"
                    @click="handleGenerateFromDrawer('clip')"
                  >
                    生成视频
                  </n-button>
                </template>
                {{ activeShot.keyframe_confirmed ? '提交视频生成任务' : '需先确认分镜图（关键帧）后才能生成视频' }}
              </n-tooltip>
            </div>
          </n-space>

          <!-- 参考图与生成配置：默认沿用原配置，只允许修改提示词 -->
          <RegenerateConfigPanel
            v-if="regenerateTarget"
            :key="`${activeShot.id}-${regenerateTarget}`"
            :shot-id="activeShot.id"
            :target="regenerateTarget"
          />
        </template>
      </n-drawer-content>
    </n-drawer>
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
.episode-chip:hover { border-color: var(--brand-primary, #6366f1); color: var(--brand-primary, #6366f1); }
.episode-chip.active { background: rgba(99, 102, 241, 0.1); border-color: var(--brand-primary, #6366f1); color: var(--brand-primary, #6366f1); font-weight: 600; }
.gate-banner { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; background: var(--bg-panel, #fff); }
.gate-banner.ok { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.06); }
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
  transition: box-shadow 0.2s;
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
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
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
</style>
