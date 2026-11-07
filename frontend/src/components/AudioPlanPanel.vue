<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, NCard, NCollapse, NCollapseItem, NEmpty, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { buildAudioPlan, getAudioPlan } from '@/api/audio'
import { GENERATION_STATUS_LABELS, REVIEW_STATUS_LABELS, TRACK_TYPE_LABELS } from '@/types/audio'
import type { AudioSegment } from '@/types/audio'

const props = defineProps<{
  projectId: string
  /** 指定集时隐藏集选择器；未指定时内置选择器（默认第 1 集） */
  episode?: number
}>()

const message = useMessage()
const queryClient = useQueryClient()
const expanded = ref<string[]>([])
const selectedEpisode = ref(1)
const episode = computed(() => props.episode ?? selectedEpisode.value)
const episodeOptions = Array.from({ length: 99 }, (_, i) => ({ label: `第 ${i + 1} 集`, value: i + 1 }))

const planQuery = useQuery({
  queryKey: computed(() => ['audio-plan', props.projectId, episode.value]),
  queryFn: () => getAudioPlan(props.projectId, episode.value),
  enabled: computed(() => !!props.projectId && episode.value > 0),
})

const buildMutation = useMutation({
  mutationFn: () => buildAudioPlan(props.projectId, episode.value),
  onSuccess: (result) => {
    message.success(`音频规划已生成：对白 ${result.dialogue} / 旁白 ${result.narration} / BGM ${result.bgm} / SFX ${result.sfx} / 环境声 ${result.ambient}`)
    queryClient.invalidateQueries({ queryKey: ['audio-plan', props.projectId, episode.value] })
  },
  onError: (e: Error) => message.error(`音频规划生成失败：${e.message}`),
})

const trackOrder = ['dialogue', 'narration', 'bgm', 'sfx', 'ambient', 'native_audio'] as const
const tracks = computed(() => {
  const data = planQuery.data.value
  if (!data) return []
  return trackOrder
    .filter((t) => (data.tracks[t] ?? []).length > 0)
    .map((t) => ({ type: t, label: TRACK_TYPE_LABELS[t], segments: data.tracks[t] ?? [] }))
})
const total = computed(() => planQuery.data.value?.total ?? 0)

function statusType(s: AudioSegment): 'default' | 'success' | 'warning' | 'error' {
  if (s.generation_status === 'failed') return 'error'
  if (s.generation_status === 'completed' && s.review_status === 'approved') return 'success'
  if (s.generation_status === 'completed') return 'warning'
  return 'default'
}

watch(total, (v) => {
  if (v > 0 && expanded.value.length === 0) {
    expanded.value = ['dialogue']
  }
})
</script>

<template>
  <NCard size="small" class="audio-plan-panel">
    <template #header>音频分析结果</template>
    <template #header-extra>
      <NSelect
        v-if="props.episode === undefined"
        v-model:value="selectedEpisode"
        :options="episodeOptions"
        size="small"
        style="width: 110px; margin-right: 8px"
      />
      <NButton size="small" :loading="buildMutation.isPending.value" @click="buildMutation.mutate()">
        {{ total > 0 ? '重建音频规划' : '生成音频规划' }}
      </NButton>
    </template>
    <NSpin v-if="planQuery.isLoading.value" size="small" />
    <NEmpty v-else-if="total === 0" description="暂无音频规划，点击右上角按钮从解析结果生成" />
    <NCollapse v-else v-model:expanded-names="expanded">
      <NCollapseItem v-for="track in tracks" :key="track.type" :name="track.type" :title="`${track.label}（${track.segments.length}）`">
        <div v-for="seg in track.segments" :key="seg.id" class="seg-row">
          <span class="seg-text">{{ seg.text || (seg.track_type === 'native_audio' ? 'H3 原生音频' : '（无文本）') }}</span>
          <NTag size="small" :type="statusType(seg)">
            {{ GENERATION_STATUS_LABELS[seg.generation_status] }} / {{ REVIEW_STATUS_LABELS[seg.review_status] }}
          </NTag>
          <NTag v-if="seg.cross_shot" size="small" type="info" :bordered="false">跨镜头</NTag>
        </div>
      </NCollapseItem>
    </NCollapse>
  </NCard>
</template>

<style scoped>
.audio-plan-panel { margin-top: 12px; }
.seg-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
.seg-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
