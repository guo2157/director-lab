<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, NEmpty, NInput, NSpin, NTag, useMessage } from 'naive-ui'
import {
  getAudioAssetUrl,
  getAudioTimeline,
  regenerateAudioSegment,
  replaceAudioSegment,
  reviewAudioAsset,
} from '@/api/audio'
import { GENERATION_STATUS_LABELS, REVIEW_STATUS_LABELS, TRACK_TYPE_LABELS } from '@/types/audio'
import type { AudioSegment } from '@/types/audio'

const props = defineProps<{
  projectId: string
  shotId: string
  episode: number
}>()

const message = useMessage()
const queryClient = useQueryClient()

const timelineQuery = useQuery({
  queryKey: computed(() => ['audio-timeline', props.projectId, props.episode]),
  queryFn: () => getAudioTimeline(props.projectId, props.episode),
  enabled: computed(() => !!props.projectId && props.episode > 0),
})

/** 当前镜头相关片段：shot_id 匹配 + 跨镜头片段（旁白/BGM/环境声）单独提示 */
const shotSegments = computed(() =>
  (timelineQuery.data.value?.items ?? []).filter((s) => s.shot_id === props.shotId),
)
const trackOrder = ['dialogue', 'narration', 'sfx', 'ambient', 'native_audio'] as const
const grouped = computed(() =>
  trackOrder
    .map((t) => ({
      type: t,
      label: TRACK_TYPE_LABELS[t],
      segments: shotSegments.value.filter((s) => s.track_type === t),
    }))
    .filter((g) => g.segments.length > 0),
)

function invalidate() {
  queryClient.invalidateQueries({ queryKey: ['audio-timeline', props.projectId, props.episode] })
}

/** 问题14 修复：loading 状态按片段独立追踪，避免一个片段提交时所有片段按钮一起转圈 */
const regenPendingId = ref('')

const regenMutation = useMutation({
  mutationFn: (args: { seg: AudioSegment; instruct?: string; emotion?: string }) =>
    regenerateAudioSegment(args.seg.id, {
      ...(args.instruct ? { instruct: args.instruct } : {}),
      ...(args.emotion ? { emotion: args.emotion } : {}),
    }),
  onSuccess: (r) => {
    message.success(`已提交重新生成任务（${r.job_id}）`)
    invalidate()
  },
  onError: (e: Error) => message.error(`重新生成失败：${e.message}`),
  onSettled: () => {
    regenPendingId.value = ''
  },
})

/** 单句情绪/语气指令编辑缓冲（按片段 id 保存，避免多个片段互相覆盖） */
const editBuffers = reactive<Record<string, { instruct: string; emotion: string; filePath: string }>>({})
const editingIds = ref<string[]>([])

function bufferOf(seg: AudioSegment) {
  if (!editBuffers[seg.id]) {
    editBuffers[seg.id] = {
      instruct: seg.instruct ?? '',
      emotion: seg.emotion ?? '',
      filePath: '',
    }
  }
  return editBuffers[seg.id]
}

// 问题15 修复：编辑缓冲在数据到达时预初始化，模板直接读 editBuffers[seg.id]，
// 不再在渲染期调用 bufferOf 写入响应式对象（渲染期副作用会引发重复渲染）
watch(
  shotSegments,
  (list) => {
    for (const s of list) {
      if (!editBuffers[s.id]) {
        editBuffers[s.id] = {
          instruct: s.instruct ?? '',
          emotion: s.emotion ?? '',
          filePath: '',
        }
      }
    }
  },
  { immediate: true },
)

function toggleEdit(seg: AudioSegment) {
  bufferOf(seg)
  const idx = editingIds.value.indexOf(seg.id)
  if (idx >= 0) editingIds.value.splice(idx, 1)
  else editingIds.value.push(seg.id)
}

function isEditing(seg: AudioSegment) {
  return editingIds.value.includes(seg.id)
}

function submitRegen(seg: AudioSegment) {
  regenPendingId.value = seg.id
  const buf = bufferOf(seg)
  regenMutation.mutate({ seg, instruct: buf.instruct.trim(), emotion: buf.emotion.trim() })
}

async function submitReplace(seg: AudioSegment) {
  const buf = bufferOf(seg)
  const path = buf.filePath.trim()
  if (!path) {
    message.warning('请填写服务器上的音频文件路径')
    return
  }
  try {
    const r = await replaceAudioSegment(seg.id, path)
    message.success(`已替换为版本 v${r.version}`)
    buf.filePath = ''
    invalidate()
  } catch (e) {
    message.error(`替换失败：${(e as Error).message}`)
  }
}

function review(seg: AudioSegment, status: 'approved' | 'rejected') {
  if (!seg.audio_asset_id) {
    message.warning('该片段尚未生成音频，无法审核')
    return
  }
  reviewAudioAsset(seg.audio_asset_id, { status, segment_id: seg.id }).then(() => {
    message.success(status === 'approved' ? '已审核通过' : '已驳回')
    invalidate()
  }).catch((e: Error) => message.error(`审核失败：${e.message}`))
}

function segStatusType(s: AudioSegment): 'default' | 'success' | 'warning' | 'error' {
  if (s.generation_status === 'failed') return 'error'
  if (s.generation_status === 'completed' && s.review_status === 'approved') return 'success'
  if (s.generation_status === 'completed') return 'warning'
  return 'default'
}
</script>

<template>
  <NSpin v-if="timelineQuery.isLoading.value" size="small" />
  <NEmpty v-else-if="shotSegments.length === 0" size="small" description="暂无音频片段，请先在剧本分析页生成音频规划" />
  <div v-else class="audio-panel">
    <div v-for="grp in grouped" :key="grp.type" class="track-group">
      <div class="track-head">{{ grp.label }}（{{ grp.segments.length }}）</div>
      <div v-for="seg in grp.segments" :key="seg.id" class="seg-item">
        <div class="seg-main">
          <span class="seg-text">{{ seg.text || (seg.track_type === 'native_audio' ? (seg.note === 'absent' ? '无音频流' : 'H3 原生音频') : '（无文本）') }}</span>
          <NTag size="small" :type="segStatusType(seg)">
            {{ GENERATION_STATUS_LABELS[seg.generation_status] }} / {{ REVIEW_STATUS_LABELS[seg.review_status] }}
          </NTag>
          <NTag v-if="seg.cross_shot" size="small" type="info" :bordered="false">跨镜头</NTag>
          <span v-if="seg.duration_ms" class="seg-dur">{{ (seg.duration_ms / 1000).toFixed(1) }}s</span>
        </div>
        <!-- 试听：已有音频资产时可直接播放 -->
        <audio
          v-if="seg.audio_asset_id"
          class="seg-player"
          controls
          preload="none"
          :src="getAudioAssetUrl(seg.audio_asset_id)"
        />
        <div v-if="seg.track_type !== 'native_audio'" class="seg-actions">
          <NButton size="tiny" :disabled="!seg.text" :loading="regenPendingId === seg.id" @click="submitRegen(seg)">重新生成</NButton>
          <NButton size="tiny" secondary @click="toggleEdit(seg)">{{ isEditing(seg) ? '收起编辑' : '编辑情绪/替换' }}</NButton>
          <NButton size="tiny" :disabled="seg.generation_status !== 'completed'" @click="review(seg, 'approved')">通过</NButton>
          <NButton size="tiny" quaternary :disabled="seg.generation_status !== 'completed'" @click="review(seg, 'rejected')">驳回</NButton>
        </div>
        <div v-if="isEditing(seg) && seg.track_type !== 'native_audio' && editBuffers[seg.id]" class="seg-editor">
          <div class="edit-row">
            <span class="edit-label">语气指令</span>
            <NInput
              v-model:value="editBuffers[seg.id].instruct"
              size="tiny"
              placeholder="如：低沉、压抑，语速稍慢（留空则用角色默认）"
            />
          </div>
          <div class="edit-row">
            <span class="edit-label">情绪</span>
            <NInput
              v-model:value="editBuffers[seg.id].emotion"
              size="tiny"
              placeholder="如：sad / angry（留空则沿用分镜情绪）"
            />
          </div>
          <div class="edit-actions">
            <NButton size="tiny" type="primary" :loading="regenPendingId === seg.id" @click="submitRegen(seg)">按此指令重新生成</NButton>
          </div>
          <div class="edit-row">
            <span class="edit-label">替换音频</span>
            <NInput
              v-model:value="editBuffers[seg.id].filePath"
              size="tiny"
              placeholder="服务器上的音频文件绝对路径，如 /data/media/xxx.wav"
            />
          </div>
          <div class="edit-actions">
            <NButton size="tiny" secondary @click="submitReplace(seg)">登记为新版本</NButton>
            <span class="edit-hint">替换会创建新资产版本，旧版本保留不可逆丢失</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.audio-panel { display: flex; flex-direction: column; gap: 12px; }
.track-group { display: flex; flex-direction: column; gap: 6px; }
.track-head { font-size: 13px; font-weight: 600; }
.seg-item { border: 1px solid rgba(128, 128, 128, 0.16); border-radius: 8px; padding: 8px 10px; }
.seg-main { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.seg-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.seg-dur { color: rgba(128, 128, 128, 0.9); font-size: 12px; }
.seg-player { display: block; width: 100%; height: 32px; margin-top: 6px; }
.seg-actions { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.seg-editor { margin-top: 8px; padding-top: 8px; border-top: 1px dashed rgba(128, 128, 128, 0.2); display: flex; flex-direction: column; gap: 6px; }
.edit-row { display: flex; align-items: center; gap: 8px; }
.edit-label { font-size: 12px; min-width: 64px; color: rgba(128, 128, 128, 0.95); }
.edit-actions { display: flex; align-items: center; gap: 8px; }
.edit-hint { font-size: 11px; color: rgba(128, 128, 128, 0.85); }
</style>
