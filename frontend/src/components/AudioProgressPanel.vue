<script setup lang="ts">
/** 导演台音频生产进度面板（2026-11-07）。
 *
 * 职责：
 * - 按对白/旁白/BGM/SFX 分组展示实时生成进度（数据库统计，不伪造）
 * - 展开查看片段明细（角色/集数/文本/状态/时长/备注）
 * - 失败片段支持局部重新生成（单条，不扩大范围）
 * - 待确认音色的对白明确提示（引导去音色匹配面板确认）
 */
import { computed, ref, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton,
  NCard,
  NEmpty,
  NPagination,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import {
  getAudioProductionProgress,
  getAudioSegmentDetails,
  regenerateAudioSegment,
} from '@/api/audio'
import { GENERATION_STATUS_LABELS, TRACK_TYPE_LABELS } from '@/types/audio'
import type { AudioSegmentDetail, AudioTrackType } from '@/types/audio'

const props = defineProps<{
  projectId: string
}>()

const message = useMessage()
const queryClient = useQueryClient()

const progressQuery = useQuery({
  queryKey: ['audio-progress', props.projectId],
  queryFn: () => getAudioProductionProgress(props.projectId),
  refetchInterval: 8000,
})

const progress = computed(() => progressQuery.data.value?.progress ?? null)
const waitingTotal = computed(() => {
  const d = progress.value?.dialogue as Record<string, number> | undefined
  return d?.waiting_voice_confirmation ?? 0
})

const trackOrder: Array<{ key: 'dialogue' | 'narration' | 'bgm' | 'sfx'; label: string }> = [
  { key: 'dialogue', label: '对白 TTS' },
  { key: 'narration', label: '旁白 TTS' },
  { key: 'bgm', label: 'BGM' },
  { key: 'sfx', label: 'SFX' },
]

function statusType(s: string): 'default' | 'success' | 'warning' | 'error' | 'info' {
  if (s === 'failed') return 'error'
  if (s === 'completed') return 'success'
  if (s === 'waiting_voice_confirmation') return 'warning'
  if (s === 'queued' || s === 'processing') return 'info'
  return 'default'
}

// ---- 明细展开 ----
const expandedTrack = ref<'dialogue' | 'narration' | 'bgm' | 'sfx' | null>(null)
const page = ref(1)
const pageSize = 10

const detailQuery = useQuery({
  queryKey: computed(() => [
    'audio-progress-detail', props.projectId, expandedTrack.value, page.value,
  ]),
  queryFn: () =>
    getAudioSegmentDetails(props.projectId, {
      track_type: expandedTrack.value ?? undefined,
      page: page.value,
      page_size: pageSize,
    }),
  enabled: computed(() => expandedTrack.value !== null),
})
const detailItems = computed<AudioSegmentDetail[]>(() => detailQuery.data.value?.items ?? [])
const detailTotal = computed(() => detailQuery.data.value?.total ?? 0)

function toggleTrack(key: 'dialogue' | 'narration' | 'bgm' | 'sfx') {
  if (expandedTrack.value === key) {
    expandedTrack.value = null
  } else {
    expandedTrack.value = key
    page.value = 1
  }
}

// ---- 局部重新生成 ----
const regenMutation = useMutation({
  mutationFn: (segmentId: string) => regenerateAudioSegment(segmentId),
  onSuccess: () => {
    message.success('已重新提交生成任务')
    queryClient.invalidateQueries({ queryKey: ['audio-progress', props.projectId] })
    queryClient.invalidateQueries({ queryKey: ['audio-progress-detail', props.projectId] })
  },
  onError: (e: Error) => message.error(`重新生成失败：${e.message}`),
})

function canRegen(seg: AudioSegmentDetail): boolean {
  return (
    (seg.track_type === 'dialogue' || seg.track_type === 'narration' || seg.track_type === 'bgm') &&
    (seg.generation_status === 'failed' || seg.generation_status === 'completed' || seg.generation_status === 'pending')
  )
}

watch(expandedTrack, () => {
  page.value = 1
})
</script>

<template>
  <NCard size="small" class="audio-progress-panel" title="音频生产">
    <template #header-extra>
      <NButton size="tiny" quaternary @click="progressQuery.refetch()">刷新</NButton>
    </template>

    <NAlert v-if="waitingTotal > 0" type="warning" :bordered="false" class="waiting-tip">
      有 {{ waitingTotal }} 条对白正在等待角色音色确认，请先在资产库角色页完成音色确认。
    </NAlert>

    <NSpin :show="progressQuery.isLoading.value">
      <NEmpty v-if="!progress" description="暂无音频规划数据（剧本分析并生成音频规划后展示）" />
      <div v-else class="progress-rows">
        <div
          v-for="t in trackOrder"
          :key="t.key"
          class="progress-row"
          :class="{ active: expandedTrack === t.key }"
          @click="toggleTrack(t.key)"
        >
          <div class="row-head">
            <span class="row-title">{{ t.label }}</span>
            <span class="row-total">总数：{{ progress[t.key]?.total ?? 0 }}</span>
          </div>
          <div class="row-status">
            <NTag
              v-for="(n, s) in progress[t.key]"
              :key="s"
              size="tiny"
              :type="statusType(String(s))"
              :bordered="false"
              class="status-tag"
            >
              {{ GENERATION_STATUS_LABELS[s as keyof typeof GENERATION_STATUS_LABELS] ?? s }} {{ n }}
            </NTag>
          </div>
        </div>
      </div>
    </NSpin>

    <div v-if="expandedTrack" class="detail-block">
      <div class="detail-title">
        {{ TRACK_TYPE_LABELS[expandedTrack as AudioTrackType] }}明细
        <span class="detail-total">（共 {{ detailTotal }} 条）</span>
      </div>
      <NSpin :show="detailQuery.isLoading.value">
        <NEmpty v-if="!detailItems.length" description="该类别暂无片段" />
        <div v-for="seg in detailItems" :key="seg.id" class="detail-row">
          <div class="detail-main">
            <NTag size="tiny" :type="statusType(seg.generation_status)" class="mr6">
              {{ GENERATION_STATUS_LABELS[seg.generation_status] }}
            </NTag>
            <span v-if="seg.character_name" class="detail-char">{{ seg.character_name }}</span>
            <span class="detail-ep">第 {{ seg.episode }} 集</span>
            <span class="detail-text">{{ seg.text || '（无文本）' }}</span>
            <span v-if="seg.duration_ms" class="detail-dur">{{ (seg.duration_ms / 1000).toFixed(1) }}s</span>
          </div>
          <div v-if="seg.note" class="detail-note">{{ seg.note }}</div>
          <div class="detail-actions">
            <NButton
              v-if="canRegen(seg)"
              size="tiny"
              :loading="regenMutation.isPending.value"
              @click.stop="regenMutation.mutate(seg.id)"
            >重新生成</NButton>
          </div>
        </div>
      </NSpin>
      <NPagination
        v-if="detailTotal > pageSize"
        v-model:page="page"
        :page-count="Math.ceil(detailTotal / pageSize)"
        size="small"
        class="detail-pagination"
      />
    </div>
  </NCard>
</template>

<style scoped>
.audio-progress-panel {
  margin-bottom: 12px;
}
.waiting-tip {
  margin-bottom: 10px;
}
.progress-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.progress-row {
  border: 1px solid var(--divider-color, #e5e5e5);
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
}
.progress-row.active {
  border-color: var(--primary-color, #18a058);
}
.row-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.row-title {
  font-weight: 600;
  font-size: 13px;
}
.row-total {
  font-size: 12px;
  color: var(--text-color-3, #999);
}
.row-status {
  margin-top: 6px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.status-tag {
  cursor: default;
}
.detail-block {
  margin-top: 10px;
  border-top: 1px dashed var(--divider-color, #e5e5e5);
  padding-top: 10px;
}
.detail-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 8px;
}
.detail-total {
  font-weight: 400;
  color: var(--text-color-3, #999);
}
.detail-row {
  border: 1px solid var(--divider-color, #e5e5e5);
  border-radius: 6px;
  padding: 6px 8px;
  margin-bottom: 6px;
}
.detail-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
}
.mr6 {
  margin-right: 2px;
}
.detail-char {
  font-weight: 600;
}
.detail-ep {
  color: var(--text-color-3, #999);
}
.detail-text {
  flex: 1;
  min-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.detail-dur {
  color: var(--text-color-3, #999);
}
.detail-note {
  font-size: 12px;
  color: var(--warning-color, #f0a020);
  margin-top: 4px;
}
.detail-actions {
  margin-top: 4px;
}
.detail-pagination {
  margin-top: 8px;
  justify-content: center;
}
</style>
