<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpace, NSpin, NEmpty, NPopconfirm, NSelect,
  NAlert, useMessage,
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
    lastError.value = ''
    message.success('合成任务已提交')
    queryClient.invalidateQueries({ queryKey: ['compositions', projectId] })
  },
  onError: (e: any) => {
    const code = e?.code
    let text = ''
    if (code === 'output_missing') {
      text = '存在缺失产物，无法合成：请先在分镜看板补齐全部分镜图与视频'
    } else if (code === 'duration_mismatch_review_required') {
      text = '存在待审核产物，需先处理后再合成：请到分镜看板完成审核'
    } else if (code === 'speaker_mapping_required') {
      text = '缺少角色配音映射，请先配置角色音色映射'
    } else {
      text = `合成失败：${e?.message ?? e}`
    }
    lastError.value = text
    message.error(text)
  },
})

/** 合成阻断判断（仅 UI 层）：已有合成任务进行中时避免重复提交 */
const hasBlockingIssues = computed(() =>
  (compositionsQuery.data.value ?? []).some((c: Composition) => c.status === 'composing'),
)
const blockingReason = computed(() =>
  hasBlockingIssues.value ? '已有合成任务进行中，请等待完成后再提交' : '',
)
/** 最近一次操作错误：页面内以 NAlert 常驻展示，避免只依赖短暂 toast */
const lastError = ref('')

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
    lastError.value = ''
    message.success('导出任务已提交')
    queryClient.invalidateQueries({ queryKey: ['exports', projectId] })
  },
  onError: (e: any) => {
    const code = e?.code
    const text =
      code === 'composition_stale'
        ? '合成版本已失效，请重新合成后再导出'
        : `导出失败：${e?.message ?? e}`
    lastError.value = text
    message.error(text)
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
    .map((c: Composition) => ({ label: `第${c.episode}集 - ${new Date(c.created_at).toLocaleString()}`, value: c.composition_id }))
)
</script>

<template>
  <div class="compose-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">合成与导出</h1>
        <span class="page-hint">全部分镜视频完成后，在此合成整集成片并导出</span>
      </div>
    </header>
    <n-alert
      v-if="lastError"
      type="error"
      closable
      class="compose-alert"
      @close="lastError = ''"
    >
      {{ lastError }}
    </n-alert>
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
            <n-empty size="small" description="暂无预览：请选择右侧合成记录" />
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
            <div class="form-actions">
              <n-button
                type="primary"
                size="medium"
                :loading="composeMutation.isPending.value"
                :disabled="hasBlockingIssues"
                @click="handleCompose"
              >开始合成</n-button>
              <span v-if="hasBlockingIssues" class="form-hint">{{ blockingReason }}</span>
            </div>
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
            <div class="form-actions">
              <n-popconfirm @positive-click="handleExport">
                <template #trigger>
                  <n-button
                    type="primary"
                    size="medium"
                    :loading="exportMutation.isPending.value"
                    :disabled="!exportCompositionId"
                  >导出 MP4</n-button>
                </template>
                确认使用当前选择的合成版本导出 MP4？
              </n-popconfirm>
              <span v-if="!exportCompositionId" class="form-hint">请先选择非失效的合成版本</span>
            </div>
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
              :key="comp.composition_id"
              class="record-item"
              :class="{ stale: comp.stale, active: previewComposition?.composition_id === comp.composition_id }"
              @click="selectPreview(comp)"
            >
              <div class="record-info">
                <span class="record-title">第{{ comp.episode }}集</span>
                <span class="record-date">{{ new Date(comp.created_at).toLocaleString() }}</span>
              </div>
              <div class="record-tags">
                <span
                  class="status-tag"
                  :class="comp.status === 'done' ? 'is-succeeded' : comp.status === 'failed' ? 'is-failed' : 'is-running'"
                >
                  {{ comp.status === 'composing' ? '合成中' : comp.status === 'done' ? '已完成' : comp.status === 'failed' ? '失败' : comp.status }}
                </span>
                <span v-if="comp.stale" class="status-tag is-stale">已失效</span>
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
                <span class="record-title">{{ exp.file_name ?? '导出文件' }}</span>
                <span class="record-date">{{ new Date(exp.created_at).toLocaleString() }}</span>
              </div>
              <div class="record-tags">
                <span
                  class="status-tag"
                  :class="exp.status === 'done' ? 'is-succeeded' : exp.status === 'failed' ? 'is-failed' : 'is-running'"
                >
                  {{ exp.status === 'exporting' ? '处理中' : exp.status === 'done' ? '已完成' : '失败' }}
                </span>
                <n-button
                  v-if="exp.status === 'done'"
                  size="small"
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
.compose-page { padding: 24px; min-height: calc(100vh - var(--app-header-height, 56px)); box-sizing: border-box; }
.compose-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.compose-hint { font-size: 13px; color: var(--text-secondary); }
.compose-alert { margin-bottom: 16px; }
.compose-layout { display: flex; gap: 24px; align-items: flex-start; }
.preview-section { flex: 1 1 60%; max-width: 60%; min-width: 0; }
.records-section { flex: 1 1 40%; max-width: 40%; min-width: 0; }
.settings-card { background: var(--bg-panel); border: 1px solid var(--border-subtle); border-radius: 12px; }
.video-preview { width: 100%; }
.video-player { width: 100%; max-height: 460px; border-radius: 8px; background: #000; display: block; }
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
.record-title { font-size: 14px; color: var(--text-primary); }
.record-date { font-size: 12px; color: var(--text-secondary); }
.record-tags { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.record-duration { font-size: 12px; color: var(--text-secondary); }
.form-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.form-hint { font-size: 13px; color: var(--text-tertiary); }
@media (max-width: 1024px) {
  .compose-layout { flex-direction: column; }
  .preview-section, .records-section { flex: 1 1 auto; max-width: 100%; width: 100%; }
}
@media (max-width: 768px) {
  .compose-page { padding: 16px; }
}
</style>
