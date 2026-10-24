<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton, NSpace, NSpin, NEmpty, NTag, NDrawer, NDrawerContent,
  NInput, NTooltip, NCheckbox, NSelect, useMessage,
} from 'naive-ui'
import { listScenes, listSceneShots, reorderSceneShots } from '@/api/script'
import { generateShot, generateSceneShots } from '@/api/storyboard'
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

async function loadSceneShots(sceneId: string) {
  loadingShots.value[sceneId] = true
  try {
    const res = await listSceneShots(sceneId)
    shotsMap.value[sceneId] = res.items
  } catch (e: any) {
    message.error(`加载镜头失败: ${e?.message ?? e}`)
  } finally {
    loadingShots.value[sceneId] = false
  }
}

watch(() => scenesQuery.data.value, (data) => {
  if (data?.items) {
    data.items.forEach((s: Scene) => loadSceneShots(s.id))
  }
}, { immediate: true })

// --- Selection & batch generate ---
const selectedShotIds = ref<Set<string>>(new Set())
const batchTarget = ref<'keyframe' | 'clip' | 'audio'>('keyframe')

function toggleSelect(shotId: string) {
  if (selectedShotIds.value.has(shotId)) {
    selectedShotIds.value.delete(shotId)
  } else {
    selectedShotIds.value.add(shotId)
  }
}

const selectedCount = computed(() => selectedShotIds.value.size)
const canBatchGenerate = computed(() => selectedCount.value >= 3)

const batchGenerateMutation = useMutation({
  mutationFn: (params: { sceneId: string; shotIds: string[]; target: string }) =>
    generateSceneShots(params.sceneId, { shot_ids: params.shotIds, target: params.target as any }),
  onSuccess: () => {
    message.success('批量生成任务已提交')
    selectedShotIds.value.clear()
    queryClient.invalidateQueries({ queryKey: ['scenes', projectId] })
  },
  onError: (e: any) => {
    message.error(`批量生成失败: ${e?.message ?? e}`)
  },
})

function handleBatchGenerate() {
  // Group selected by scene
  const byScene: Record<string, string[]> = {}
  for (const [sceneId, shots] of Object.entries(shotsMap.value)) {
    for (const shot of shots) {
      if (selectedShotIds.value.has(shot.id)) {
        if (!byScene[sceneId]) byScene[sceneId] = []
        byScene[sceneId].push(shot.id)
      }
    }
  }
  const entries = Object.entries(byScene)
  if (entries.length === 0) return
  if (entries.length === 1) {
    const [sceneId, shotIds] = entries[0]
    batchGenerateMutation.mutate({ sceneId, shotIds, target: batchTarget.value })
  } else {
    message.warning('批量生成仅支持同一场景内的镜头，请重新选择')
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

function handleGenerate(shot: Shot) {
  generateMutation.mutate({ shotId: shot.id, payload: { target: 'keyframe' } })
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
  reorderSceneShots(sceneId, reordered.map(s => s.id)).catch((e: any) => {
    message.error(`排序失败: ${e?.message ?? e}`)
    loadSceneShots(sceneId)
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

function openDrawer(shot: Shot) {
  activeShot.value = shot
  editImagePrompt.value = shot.image_prompt ?? ''
  editVideoPrompt.value = shot.video_prompt ?? ''
  editNegativePrompt.value = ''
  drawerVisible.value = true
}

// --- Status badge ---
interface StatusInfo { label: string; color: string; icon?: string }
function getShotStatus(shot: Shot): StatusInfo {
  const ca = shot.current_assets
  const has = ca?.current_assets && (ca.current_assets.keyframe || ca.current_assets.clip || ca.current_assets.audio)
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

const targetOptions = [
  { label: '关键帧', value: 'keyframe' },
  { label: '视频片段', value: 'clip' },
  { label: '音频', value: 'audio' },
]
</script>

<template>
  <div class="storyboard-page">
    <!-- Batch bar -->
    <div class="batch-bar">
      <n-space align="center">
        <span class="batch-info">已选 {{ selectedCount }} 个镜头</span>
        <n-select v-model:value="batchTarget" :options="targetOptions" size="small" style="width: 120px" />
        <n-tooltip>
          <template #trigger>
            <n-button
              size="small"
              type="primary"
              :disabled="!canBatchGenerate"
              :loading="batchGenerateMutation.isPending.value"
              @click="handleBatchGenerate"
            >
              批量生成
            </n-button>
          </template>
          {{ canBatchGenerate ? '提交批量生成任务' : '需选中至少 3 个镜头' }}
        </n-tooltip>
        <n-button size="small" quaternary @click="selectedShotIds.clear()">清除选择</n-button>
      </n-space>
    </div>

    <!-- Loading -->
    <n-spin v-if="scenesQuery.isLoading.value" class="page-loading" />

    <!-- Empty -->
    <n-empty v-else-if="scenesQuery.data.value?.items?.length === 0" description="暂无场景，请先在剧本页解析剧本">
      <template #extra>
        <n-button type="primary" @click="router.push(`/project/${projectId}/script`)">前往剧本页</n-button>
      </template>
    </n-empty>

    <!-- Scenes -->
    <div v-else class="scenes-container">
      <div v-for="scene in scenesQuery.data.value?.items || []" :key="scene.id" class="scene-group">
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
              <n-checkbox
                :checked="selectedShotIds.has(shot.id)"
                @update:checked="toggleSelect(shot.id)"
                class="shot-checkbox"
              />
              <div class="shot-card" @click="openDrawer(shot)">
                <div class="shot-thumb">
                  <img
                    v-if="shot.current_assets?.current_assets?.keyframe?.thumb_url || shot.current_assets?.current_assets?.keyframe?.url"
                    :src="shot.current_assets.current_assets.keyframe!.thumb_url || shot.current_assets.current_assets.keyframe!.url"
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
    <n-drawer v-model:show="drawerVisible" :width="480">
      <n-drawer-content title="镜头详情" closable>
        <template v-if="activeShot">
          <div class="drawer-preview">
            <img
              v-if="activeShot.current_assets?.current_assets?.keyframe?.url"
              :src="activeShot.current_assets.current_assets.keyframe!.url"
              alt="preview"
              class="preview-img"
            />
            <div v-else class="preview-placeholder">暂无预览图</div>
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
              <label class="field-label">视频提示词</label>
              <n-input v-model:value="editVideoPrompt" type="textarea" :rows="3" />
            </div>
            <div class="drawer-actions">
              <n-button
                type="primary"
                :loading="generateMutation.isPending.value"
                @click="handleGenerate(activeShot)"
              >
                生成关键帧
              </n-button>
              <n-button
                :loading="generateMutation.isPending.value"
                @click="generateMutation.mutate({ shotId: activeShot.id, payload: { target: 'clip' } })"
              >
                生成视频
              </n-button>
            </div>
          </n-space>
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
.batch-bar {
  margin-bottom: 16px;
  padding: 8px 12px;
  background: var(--bg-panel);
  border-radius: 8px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.batch-info {
  font-size: 13px;
  color: var(--text-secondary);
}
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
.shot-checkbox {
  position: absolute;
  top: 4px;
  left: 4px;
  z-index: 2;
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
.drawer-actions {
  display: flex;
  gap: 8px;
}

/* ===== 窄屏适配 ===== */
@media (max-width: 768px) {
  .storyboard-page {
    padding: 12px;
  }
  .scenes-container {
    gap: 16px;
  }
  .shot-card {
    width: 160px;
    height: 250px;
  }
  .shot-thumb {
    height: 140px;
  }
  .shot-info {
    padding: 6px 8px;
  }
}
</style>
