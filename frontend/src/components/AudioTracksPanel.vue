<script setup lang="ts">
import { computed } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NCard, NEmpty, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { getAudioConfig, getAudioTimeline, setAudioConfig } from '@/api/audio'
import {
  GENERATION_STATUS_LABELS,
  NATIVE_AUDIO_POLICY_OPTIONS,
  REVIEW_STATUS_LABELS,
  TRACK_TYPE_LABELS,
} from '@/types/audio'
import type { AudioSegment, AudioTrackType, NativeAudioPolicy } from '@/types/audio'

const props = defineProps<{
  projectId: string
  episode: number
}>()

const message = useMessage()
const queryClient = useQueryClient()

const timelineQuery = useQuery({
  queryKey: computed(() => ['audio-timeline', props.projectId, props.episode]),
  queryFn: () => getAudioTimeline(props.projectId, props.episode),
  enabled: computed(() => !!props.projectId && props.episode > 0),
})

const trackOrder: AudioTrackType[] = ['dialogue', 'narration', 'bgm', 'sfx', 'ambient', 'native_audio']

/** 原生音频策略：读取项目级持久化配置（与合成执行器同一数据源，避免前后端口径不一致） */
const configQuery = useQuery({
  queryKey: computed(() => ['audio-config', props.projectId]),
  queryFn: () => getAudioConfig(props.projectId),
  enabled: computed(() => !!props.projectId),
})

const nativePolicy = computed<NativeAudioPolicy>(() =>
  configQuery.data.value?.audio_config.native_audio_policy ?? 'discard',
)

const nativePolicyOptions = NATIVE_AUDIO_POLICY_OPTIONS.map((o) => ({ label: o.label, value: o.value }))

async function onNativePolicyChange(v: NativeAudioPolicy) {
  try {
    await setAudioConfig(props.projectId, { native_audio_policy: v })
    await queryClient.invalidateQueries({ queryKey: ['audio-config', props.projectId] })
    message.success('原生音轨策略已保存')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  }
}

const tracks = computed(() => {
  const items = timelineQuery.data.value?.items ?? []
  return trackOrder.map((t) => {
    const segs = items.filter((s) => s.track_type === t)
    return {
      type: t,
      label: TRACK_TYPE_LABELS[t],
      segments: segs,
      total: segs.length,
      completed: segs.filter((s) => s.generation_status === 'completed').length,
      approved: segs.filter((s) => s.review_status === 'approved').length,
      pending: segs.filter((s) => s.review_status === 'pending' && s.generation_status === 'completed').length,
      missing: segs.filter((s) => s.generation_status !== 'completed' && s.track_type !== 'native_audio').length,
    }
  })
})

/** 阻断提示：必需轨（对白/旁白）有未生成片段时提示 */
const requiredMissing = computed(() =>
  tracks.value
    .filter((t) => (t.type === 'dialogue' || t.type === 'narration') && t.missing > 0)
    .map((t) => `${t.label} ${t.missing} 条未生成`),
)

function segStatusType(s: AudioSegment): 'default' | 'success' | 'warning' | 'error' {
  if (s.generation_status === 'failed') return 'error'
  if (s.generation_status === 'completed' && s.review_status === 'approved') return 'success'
  if (s.generation_status === 'completed') return 'warning'
  return 'default'
}

function refresh() {
  queryClient.invalidateQueries({ queryKey: ['audio-timeline', props.projectId, props.episode] })
  message.success('音频轨道状态已刷新')
}
</script>

<template>
  <NCard size="small" title="音频轨道" class="audio-tracks-panel">
    <template #header-extra>
      <NButton size="tiny" quaternary @click="refresh">刷新</NButton>
    </template>
    <NSpin v-if="timelineQuery.isLoading.value" size="small" />
    <NEmpty v-else-if="(timelineQuery.data.value?.total ?? 0) === 0" size="small" description="暂无音频规划，请先在剧本分析页生成音频规划" />
    <div v-else class="tracks-list">
      <div v-for="t in tracks" :key="t.type" class="track-row">
        <div class="track-info">
          <span class="track-name">{{ t.label }}</span>
          <NTag v-if="t.total === 0" size="small" :bordered="false">无</NTag>
          <template v-else>
            <span class="track-count">{{ t.total }} 条</span>
            <NTag v-if="t.missing" size="small" type="warning" :bordered="false">{{ t.missing }} 未生成</NTag>
            <NTag v-if="t.pending" size="small" type="info" :bordered="false">{{ t.pending }} 待审核</NTag>
            <NTag v-if="t.approved" size="small" type="success" :bordered="false">{{ t.approved }} 已通过</NTag>
          </template>
        </div>
        <!-- 原生音频策略选择 -->
        <NSelect
          v-if="t.type === 'native_audio' && t.total > 0"
          :value="nativePolicy"
          :options="nativePolicyOptions"
          size="small"
          style="width: 200px"
          @update:value="onNativePolicyChange"
        />
      </div>
      <!-- 片段明细（折叠简表） -->
      <div v-for="t in tracks" :key="`detail-${t.type}`" class="track-detail">
        <div v-for="seg in t.segments" :key="seg.id" class="seg-line">
          <span class="seg-text">{{ seg.text || (seg.track_type === 'native_audio' ? (seg.note === 'absent' ? '无音频流' : 'H3 原生音频') : '（无文本）') }}</span>
          <NTag size="small" :type="segStatusType(seg)">
            {{ GENERATION_STATUS_LABELS[seg.generation_status] }} / {{ REVIEW_STATUS_LABELS[seg.review_status] }}
          </NTag>
          <span v-if="seg.duration_ms" class="seg-dur">{{ (seg.duration_ms / 1000).toFixed(1) }}s</span>
        </div>
      </div>
      <div v-if="requiredMissing.length" class="track-warning">
        ⚠ 必需音频未就绪：{{ requiredMissing.join('；') }}。合成将按现有素材混音，未生成片段将静音处理。
      </div>
    </div>
  </NCard>
</template>

<style scoped>
.audio-tracks-panel { margin-top: 12px; }
.tracks-list { display: flex; flex-direction: column; gap: 8px; }
.track-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.track-info { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.track-name { font-weight: 600; min-width: 88px; }
.track-count { color: rgba(128, 128, 128, 0.9); font-size: 12px; }
.track-detail { border-top: 1px dashed rgba(128, 128, 128, 0.15); padding-top: 6px; }
.seg-line { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
.seg-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.seg-dur { color: rgba(128, 128, 128, 0.9); font-size: 12px; }
.track-warning { color: #d97706; font-size: 12px; padding: 6px 0 0; }
</style>
