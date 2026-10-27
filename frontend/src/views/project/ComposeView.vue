<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpace, NSpin, NEmpty, NTag, NPopconfirm, NSelect,
  NTooltip, useMessage,
} from 'naive-ui'
import {
  composeProject, listCompositions, exportProject, listExports, downloadExportUrl,
} from '@/api/compose'
import type { Composition, ComposePayload, ExportPayload } from '@/types/compose'

const route = useRoute()
const message = useMessage()
const queryClient = useQueryClient()

const projectId = computed(() => route.params.id as string)

// --- Compositions ---
const compositionsQuery = useQuery({
  queryKey: ['compositions', projectId],
  queryFn: () => listCompositions(projectId.value),
})

// --- Exports ---
const exportsQuery = useQuery({
  queryKey: ['exports', projectId],
  queryFn: () => listExports(projectId.value),
})

// --- Compose ---
const composeEpisode = ref<number | null>(null)
const composeSubtitle = ref(true)
const composeSubtitleMode = ref<'burn' | 'sidecar'>('burn')
const composeMutation = useMutation({
  mutationFn: (payload: ComposePayload) => composeProject(projectId.value, payload),
  onSuccess: () => {
    message.success('合成任务已提交')
    queryClient.invalidateQueries({ queryKey: ['compositions', projectId] })
  },
  onError: (e: any) => {
    const code = e?.code
    if (code === 'output_missing') {
      message.error('存在缺失产物，无法合成')
    } else if (code === 'duration_mismatch_review_required') {
      message.error('存在待审核产物，需先处理后再合成')
    } else if (code === 'speaker_mapping_required') {
      message.error('缺少角色配音映射，请先配置')
    } else {
      message.error(`合成失败: ${e?.message ?? e}`)
    }
  },
})

// Check if compose should be disabled
const hasBlockingIssues = computed(() => {
  // This would ideally check for pending review or missing assets
  // For now, allow compose but handle errors via mutation
  return false
})

function handleCompose() {
  composeMutation.mutate({
    episode: composeEpisode.value ?? undefined,
    include_subtitle: composeSubtitle.value,
    subtitle_mode: composeSubtitleMode.value,
  })
}

// --- Export ---
const exportCompositionId = ref<string | null>(null)
const exportFormat = ref('mp4')
const exportProfile = ref<'standard' | 'high_quality'>('standard')

const exportMutation = useMutation({
  mutationFn: (payload: ExportPayload) => exportProject(projectId.value, payload),
  onSuccess: () => {
    message.success('导出任务已提交')
    queryClient.invalidateQueries({ queryKey: ['exports', projectId] })
  },
  onError: (e: any) => {
    const code = e?.code
    if (code === 'composition_stale') {
      message.error('合成版本已失效，请重新合成')
    } else {
      message.error(`导出失败: ${e?.message ?? e}`)
    }
  },
})

function handleExport() {
  if (!exportCompositionId.value) {
    message.warning('请选择合成版本')
    return
  }
  exportMutation.mutate({
    composition_id: exportCompositionId.value,
    format: exportFormat.value,
    delivery_profile: exportProfile.value,
  })
}

function handleDownload(exportId: string) {
  window.open(downloadExportUrl(exportId), '_blank')
}

// --- Preview ---
const previewComposition = ref<Composition | null>(null)

function selectPreview(comp: Composition) {
  previewComposition.value = comp
}

const subtitleModeOptions = [
  { label: '内嵌字幕', value: 'burn' },
  { label: '外挂字幕', value: 'sidecar' },
]
const profileOptions = [
  { label: '标准质量', value: 'standard' },
  { label: '高质量', value: 'high_quality' },
]

const compositionOptions = computed(() =>
  (compositionsQuery.data.value ?? [])
    .filter((c: Composition) => !c.stale)
    .map((c: Composition) => ({ label: `第${c.episode}集 - ${new Date(c.created_at).toLocaleString()}`, value: c.id }))
)
</script>

<template>
  <div class="compose-page">
    <div class="compose-layout">
      <!-- Left: Preview -->
      <div class="preview-section">
        <n-card title="视频预览" class="settings-card">
          <div v-if="previewComposition?.file_path" class="video-preview">
            <video controls preload="metadata" class="video-player">
              <source :src="`/api/media/${previewComposition.file_path}`" type="video/mp4" />
            </video>
          </div>
          <div v-else class="preview-placeholder">
            <span>选择合成记录进行预览</span>
          </div>
        </n-card>

        <!-- Compose form -->
        <n-card title="合成" class="settings-card" style="margin-top: 16px">
          <n-space vertical :size="12">
            <div>
              <label class="field-label">集数（可选）</label>
              <n-input-number v-model:value="composeEpisode" :min="1" placeholder="留空合成全部" style="width: 100%" />
            </div>
            <div>
              <label class="field-label">字幕</label>
              <n-space align="center">
                <n-switch v-model:value="composeSubtitle" size="small" />
                <n-select v-model:value="composeSubtitleMode" :options="subtitleModeOptions" size="small" style="width: 120px" :disabled="!composeSubtitle" />
              </n-space>
            </div>
            <n-tooltip>
              <template #trigger>
                <n-button
                  type="primary"
                  :loading="composeMutation.isPending.value"
                  :disabled="hasBlockingIssues"
                  @click="handleCompose"
                >开始合成</n-button>
              </template>
              {{ hasBlockingIssues ? '存在待审核或缺失产物，无法合成' : '提交合成任务' }}
            </n-tooltip>
          </n-space>
        </n-card>

        <!-- Export form -->
        <n-card title="导出" class="settings-card" style="margin-top: 16px">
          <n-space vertical :size="12">
            <div>
              <label class="field-label">选择合成版本</label>
              <n-select v-model:value="exportCompositionId" :options="compositionOptions" placeholder="选择非失效的合成版本" />
            </div>
            <div>
              <label class="field-label">导出质量</label>
              <n-select v-model:value="exportProfile" :options="profileOptions" />
            </div>
            <n-popconfirm @positive-click="handleExport">
              <template #trigger>
                <n-button
                  type="primary"
                  :loading="exportMutation.isPending.value"
                  :disabled="!exportCompositionId"
                >导出 MP4</n-button>
              </template>
              确认导出？
            </n-popconfirm>
          </n-space>
        </n-card>
      </div>

      <!-- Right: Records -->
      <div class="records-section">
        <!-- Compositions -->
        <n-card title="合成记录" class="settings-card">
          <n-spin v-if="compositionsQuery.isLoading.value" size="small" />
          <n-empty v-else-if="(compositionsQuery.data.value ?? []).length === 0" description="暂无合成记录" size="small" />
          <div v-else class="record-list">
            <div
              v-for="comp in (compositionsQuery.data.value ?? [])"
              :key="comp.id"
              class="record-item"
              :class="{ stale: comp.stale, active: previewComposition?.id === comp.id }"
              @click="selectPreview(comp)"
            >
              <div class="record-info">
                <span class="record-title">第{{ comp.episode }}集</span>
                <span class="record-date">{{ new Date(comp.created_at).toLocaleString() }}</span>
              </div>
              <div class="record-tags">
                <n-tag size="tiny" :type="comp.status === 'completed' ? 'success' : comp.status === 'failed' ? 'error' : 'info'" round>
                  {{ comp.status === 'composing' ? '合成中' : comp.status === 'completed' ? '已完成' : comp.status === 'failed' ? '失败' : comp.status }}
                </n-tag>
                <n-tag v-if="comp.stale" size="tiny" type="warning" round>已失效</n-tag>
                <span v-if="comp.duration" class="record-duration">{{ comp.duration }}s</span>
              </div>
            </div>
          </div>
        </n-card>

        <!-- Exports -->
        <n-card title="导出记录" class="settings-card" style="margin-top: 16px">
          <n-spin v-if="exportsQuery.isLoading.value" size="small" />
          <n-empty v-else-if="(exportsQuery.data.value ?? []).length === 0" description="暂无导出记录" size="small" />
          <div v-else class="record-list">
            <div
              v-for="exp in (exportsQuery.data.value ?? [])"
              :key="exp.id"
              class="record-item"
            >
              <div class="record-info">
                <span class="record-title">{{ exp.format.toUpperCase() }} · {{ exp.delivery_profile === 'high_quality' ? '高质量' : '标准' }}</span>
                <span class="record-date">{{ new Date(exp.created_at).toLocaleString() }}</span>
              </div>
              <div class="record-tags">
                <n-tag size="tiny" :type="exp.status === 'completed' ? 'success' : exp.status === 'failed' ? 'error' : 'info'" round>
                  {{ exp.status === 'pending' ? '处理中' : exp.status === 'completed' ? '已完成' : '失败' }}
                </n-tag>
                <n-button
                  v-if="exp.status === 'completed'"
                  size="tiny"
                  quaternary
                  type="primary"
                  @click="handleDownload(exp.id)"
                >下载</n-button>
              </div>
            </div>
          </div>
        </n-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.compose-page { padding: 16px; min-height: calc(100vh - var(--app-header-height, 56px)); }
.compose-layout { display: flex; gap: 16px; }
.preview-section { flex: 1; max-width: 60%; }
.records-section { flex: 1; max-width: 40%; }
.settings-card { background: var(--bg-panel); border: 1px solid var(--border-subtle); border-radius: 12px; }
.video-preview { width: 100%; }
.video-player { width: 100%; border-radius: 8px; }
.preview-placeholder { width: 100%; height: 240px; background: var(--bg-elevated); border-radius: 8px; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); }
.field-label { display: block; font-size: 13px; color: var(--text-secondary); margin-bottom: 4px; }
.record-list { display: flex; flex-direction: column; gap: 8px; }
.record-item {
  padding: 10px 12px; background: var(--bg-elevated); border-radius: 8px; cursor: pointer;
  transition: all 0.2s; border: 1px solid transparent;
}
.record-item:hover { border-color: #3A3A45; }
.record-item.active { border-color: var(--brand-primary); }
.record-item.stale { opacity: 0.5; }
.record-info { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.record-title { font-size: 13px; color: var(--text-primary); }
.record-date { font-size: 12px; color: var(--text-secondary); }
.record-tags { display: flex; align-items: center; gap: 8px; }
.record-duration { font-size: 12px; color: var(--text-secondary); }
@media (max-width: 768px) {
  .compose-layout { flex-direction: column; }
  .preview-section, .records-section { max-width: 100%; }
}
</style>
