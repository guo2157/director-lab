<script setup lang="ts">
/** 批量角色音色智能匹配面板（2026-11-07）。
 *
 * 职责：
 * - 展示系统默认音色库（9 个：男 5 / 女 4，speaker_id 来自后端 workflow meta，不硬编码）
 * - 一键批量自动匹配（LLM 画像 + 程序评分 + 全局分配；锁定角色后端强制排除）
 * - 逐角色试听（统一测试句）、确认（可从候选中更换）、锁定/解锁
 * - 匹配结果仅写候选，需用户确认后对白才放行生成
 */
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useQuery, useMutation } from '@tanstack/vue-query'
import {
  NAlert,
  NButton,
  NCard,
  NCollapse,
  NCollapseItem,
  NEmpty,
  NSpin,
  NTag,
  useMessage,
} from 'naive-ui'
import {
  confirmVoiceBinding,
  getAudioAssetUrl,
  getVoiceBinding,
  listSystemVoices,
  lockVoiceBinding,
  matchProjectVoices,
  previewSystemVoice,
  unlockVoiceBinding,
} from '@/api/audio'
import { getJob } from '@/api/jobs'
import type { SystemVoice, VoiceMatchItem } from '@/types/audio'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const props = defineProps<{
  projectId: string
}>()

const message = useMessage()
const confirmDialog = useConfirmDialog()


// ---- 系统音色库 ----
const voicesQuery = useQuery({
  queryKey: ['system-voices', props.projectId],
  queryFn: () => listSystemVoices(),
})
const systemVoices = computed<SystemVoice[]>(() => voicesQuery.data.value?.items ?? [])
const maleVoices = computed(() => systemVoices.value.filter((v) => v.gender_presentation === 'male'))
const femaleVoices = computed(() => systemVoices.value.filter((v) => v.gender_presentation === 'female'))

// ---- 绑定状态（逐角色查询，聚合到卡片） ----
const bindings = ref<Record<string, { status: string; voice_name: string; match_score: number | null }>>({})
const bindingsLoading = ref(false)
async function refreshBindings() {
  bindingsLoading.value = true
  try {
    const ids = matched.value.map((m) => m.character_id)
    const results = await Promise.allSettled(
      ids.map(async (cid) => {
        const r = await getVoiceBinding(props.projectId, cid)
        return { cid, binding: r.binding }
      }),
    )
    const next: typeof bindings.value = {}
    for (const r of results) {
      if (r.status === 'fulfilled' && r.value.binding) {
        next[r.value.cid] = {
          status: r.value.binding.status,
          voice_name: r.value.binding.voice_name,
          match_score: r.value.binding.match_score,
        }
      }
    }
    bindings.value = next
  } finally {
    bindingsLoading.value = false
  }
}

// ---- 批量匹配 ----
const matchMutation = useMutation({
  mutationFn: () => matchProjectVoices(props.projectId),
  onSuccess: async (result) => {
    const skipNote = result.skipped.length ? `；跳过 ${result.skipped.length} 个（${result.skipped.map((s) => `${s.name}：${s.reason}`).join('、')}）` : ''
    message.success(`匹配完成：${result.total} 个角色已生成音色候选${skipNote}`)
    matched.value = result.matched
    await refreshBindings()
  },
  onError: (e: Error) => message.error(`自动匹配失败：${e.message}`),
})
const matched = ref<VoiceMatchItem[]>([])

function statusType(status: string | undefined): 'default' | 'success' | 'warning' | 'info' {
  if (status === 'locked') return 'info'
  if (status === 'confirmed') return 'success'
  if (status === 'pending') return 'warning'
  return 'default'
}
function statusLabel(status: string | undefined): string {
  if (status === 'locked') return '已锁定'
  if (status === 'confirmed') return '已确认'
  if (status === 'pending') return '待确认'
  return '—'
}

// ---- 试听（按角色推荐音色的 speaker 走系统音色缓存） ----
const playingCode = ref<string | null>(null)
let currentAudio: HTMLAudioElement | null = null
const previewingCodes = ref<Set<string>>(new Set())

async function previewBySpeaker(speakerId: string) {
  const voice = systemVoices.value.find((v) => v.speaker_id === speakerId)
  if (!voice) {
    message.warning('该音色不在系统音色库中，无法试听')
    return
  }
  await doPreview(voice.voice_code)
}

async function doPreview(voiceCode: string) {
  if (playingCode.value === voiceCode && currentAudio) {
    currentAudio.pause()
    currentAudio = null
    playingCode.value = null
    return
  }
  previewingCodes.value.add(voiceCode)
  try {
    const r = await previewSystemVoice(voiceCode, { project_id: props.projectId })
    if (r.cached && r.audio_url) {
      play(voiceCode, r.audio_url)
      return
    }
    if (r.job_id) {
      message.info('试听音频生成中，请稍候…')
      await pollPreviewJob(voiceCode, r.job_id, 0)
    }
  } catch (e) {
    message.error(`试听失败：${(e as Error).message}`)
  } finally {
    previewingCodes.value.delete(voiceCode)
  }
}

async function pollPreviewJob(voiceCode: string, jobId: string, attempt: number) {
  if (attempt > 60) {
    message.warning('试听任务耗时较长，请稍后在导演台查看结果')
    return
  }
  await new Promise((r) => setTimeout(r, 3000))
  try {
    const job = await getJob(jobId)
    if (job.status === 'succeeded') {
      const assetId = (job.result as { asset_id?: string } | null)?.asset_id
      if (assetId) {
        play(voiceCode, getAudioAssetUrl(assetId))
        return
      }
      message.warning('试听任务完成但未找到音频资产')
      return
    }
    if (job.status === 'failed') {
      message.error('试听音频生成失败，请稍后重试')
      return
    }
    await pollPreviewJob(voiceCode, jobId, attempt + 1)
  } catch {
    await pollPreviewJob(voiceCode, jobId, attempt + 1)
  }
}

function play(voiceCode: string, url: string) {
  currentAudio?.pause()
  const audio = new Audio(url)
  currentAudio = audio
  playingCode.value = voiceCode
  audio.onended = () => {
    playingCode.value = null
    currentAudio = null
  }
  audio.play().catch(() => message.error('音频播放失败'))
}

onBeforeUnmount(() => {
  currentAudio?.pause()
  currentAudio = null
})

// ---- 确认 / 锁定 ----
function confirmCharacter(m: VoiceMatchItem, voiceLibraryId?: string) {
  const hasGenerated = false // 后端在确认接口中放行片段并返回数量；此处仅展示确认意图
  confirmDialog({
    title: '确认音色',
    message: `将角色「${m.character_name}」的音色确认为「${m.voice_name}」（匹配度 ${m.match_score}%）。确认后该角色对白将进入生成队列。`,
    impact: [`角色「${m.character_name}」`, `音色「${m.voice_name}」（匹配度 ${m.match_score}%）`],
    confirmText: '确认',
    danger: false,
    onConfirm: async () => {
      try {
        const r = await confirmVoiceBinding(props.projectId, m.character_id, {
          voice_library_id: voiceLibraryId,
        })
        message.success(
          `已确认「${m.character_name}」音色为「${r.voice_name}」${r.released_segments ? `，放行 ${r.released_segments} 条等待中的对白` : ''}`,
        )
        await refreshBindings()
      } catch (e) {
        message.error(`确认失败：${(e as Error).message}`)
      }
    },
  })
  void hasGenerated
}

async function toggleLock(m: VoiceMatchItem) {
  const cur = bindings.value[m.character_id]
  try {
    if (cur?.status === 'locked') {
      await unlockVoiceBinding(props.projectId, m.character_id)
      message.success(`已解锁「${m.character_name}」音色`)
    } else {
      await lockVoiceBinding(props.projectId, m.character_id)
      message.success(`已锁定「${m.character_name}」音色（自动匹配不会覆盖）`)
    }
    await refreshBindings()
  } catch (e) {
    message.error(`操作失败：${(e as Error).message}`)
  }
}

function confirmAllPending() {
  const pending = matched.value.filter((m) => {
    const s = bindings.value[m.character_id]?.status
    return s !== 'confirmed' && s !== 'locked'
  })
  if (!pending.length) {
    message.info('没有待确认的角色（已确认或已锁定的角色不受影响）')
    return
  }
  confirmDialog({
    title: '批量确认音色',
    message: `将确认 ${pending.length} 个未锁定角色的音色（已确认/已锁定的角色不会被覆盖）。确认后对应对白将进入生成队列。`,
    impact: pending.map((m) => `「${m.character_name}」→「${m.voice_name}」`),
    confirmText: `确认 ${pending.length} 个角色`,
    danger: false,
    onConfirm: async () => {
      let ok = 0
      let fail = 0
      for (const m of pending) {
        try {
          await confirmVoiceBinding(props.projectId, m.character_id)
          ok += 1
        } catch {
          fail += 1
        }
      }
      message[fail ? 'warning' : 'success'](`批量确认完成：成功 ${ok}${fail ? `，失败 ${fail}` : ''}`)
      await refreshBindings()
    },
  })
}

watch(matched, (v) => {
  if (v.length) void refreshBindings()
})
</script>

<template>
  <NCard size="small" class="voice-match-panel" title="角色音色智能匹配">
    <template #header-extra>
      <div class="header-actions">
        <NButton size="small" type="primary" :loading="matchMutation.isPending.value" @click="matchMutation.mutate()">
          自动匹配音色
        </NButton>
        <NButton size="small" :disabled="!matched.length" @click="confirmAllPending">
          确认全部未锁定角色
        </NButton>
      </div>
    </template>

    <NAlert type="info" :bordered="false" class="tip">
      系统默认音色 {{ voicesQuery.data.value?.total ?? 0 }} 个（男 {{ voicesQuery.data.value?.male_count ?? 0 }} / 女 {{ voicesQuery.data.value?.female_count ?? 0 }}），允许多个角色复用同一音色；已锁定的音色不会被自动匹配覆盖。
    </NAlert>

    <NSpin :show="voicesQuery.isLoading.value || bindingsLoading">
      <NEmpty v-if="!systemVoices.length" description="系统音色库为空，请先在后端执行 seed_voice_library.py" />
      <template v-else>
        <NCollapse class="voice-groups">
          <NCollapseItem title="男性音色" :name="'male'">
            <div class="voice-grid">
              <div v-for="v in maleVoices" :key="v.voice_code" class="voice-card">
                <div class="voice-name">{{ v.name }}<NTag v-if="!v.enabled" size="tiny" type="warning" class="ml6">已停用</NTag></div>
                <div class="voice-desc">{{ v.description }}</div>
                <div class="voice-actions">
                  <NButton size="tiny" :loading="previewingCodes.has(v.voice_code)" @click="doPreview(v.voice_code)">
                    {{ playingCode === v.voice_code ? '停止' : '试听' }}
                  </NButton>
                </div>
              </div>
            </div>
          </NCollapseItem>
          <NCollapseItem title="女性音色" :name="'female'">
            <div class="voice-grid">
              <div v-for="v in femaleVoices" :key="v.voice_code" class="voice-card">
                <div class="voice-name">{{ v.name }}<NTag v-if="!v.enabled" size="tiny" type="warning" class="ml6">已停用</NTag></div>
                <div class="voice-desc">{{ v.description }}</div>
                <div class="voice-actions">
                  <NButton size="tiny" :loading="previewingCodes.has(v.voice_code)" @click="doPreview(v.voice_code)">
                    {{ playingCode === v.voice_code ? '停止' : '试听' }}
                  </NButton>
                </div>
              </div>
            </div>
          </NCollapseItem>
        </NCollapse>

        <NEmpty v-if="!matched.length && !matchMutation.isPending.value" description="点击「自动匹配音色」为项目角色生成音色候选" class="mt12" />
        <div v-for="m in matched" :key="m.character_id" class="match-row">
          <div class="match-main">
            <span class="char-name">{{ m.character_name }}</span>
            <NTag size="small" :type="statusType(bindings[m.character_id]?.status)">
              {{ statusLabel(bindings[m.character_id]?.status) }}
            </NTag>
            <span class="voice-name">{{ bindings[m.character_id]?.voice_name || m.voice_name }}</span>
            <NTag v-if="m.reused" size="tiny" :bordered="false">复用</NTag>
            <span class="score">匹配度 {{ m.match_score }}%</span>
          </div>
          <div class="match-reason">{{ m.match_reason }}</div>
          <div class="match-actions">
            <NButton size="tiny" @click="previewBySpeaker(m.speaker_id)">试听</NButton>
            <NButton
              v-if="bindings[m.character_id]?.status !== 'confirmed' && bindings[m.character_id]?.status !== 'locked'"
              size="tiny" type="primary" @click="confirmCharacter(m)"
            >确认</NButton>
            <NButton size="tiny" @click="toggleLock(m)">
              {{ bindings[m.character_id]?.status === 'locked' ? '解锁' : '锁定' }}
            </NButton>
          </div>
          <div v-if="m.alternatives?.length && bindings[m.character_id]?.status !== 'locked'" class="alts">
            <span class="alts-title">其他推荐：</span>
            <span v-for="a in m.alternatives" :key="a.voice_library_id" class="alt-item">
              {{ a.name }}（{{ a.match_score }}%）
              <NButton size="tiny" text type="primary" @click="previewBySpeaker(a.speaker_id)">试听</NButton>
              <NButton
                v-if="bindings[m.character_id]?.status !== 'confirmed'"
                size="tiny" text type="primary"
                @click="confirmCharacter(m, a.voice_library_id)"
              >选择</NButton>
            </span>
          </div>
        </div>
      </template>
    </NSpin>
  </NCard>
</template>

<style scoped>
.voice-match-panel {
  margin-bottom: 12px;
}
.header-actions {
  display: flex;
  gap: 8px;
}
.tip {
  margin-bottom: 12px;
}
.voice-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
}
.voice-card {
  border: 1px solid var(--divider-color);
  border-radius: 6px;
  padding: 8px 10px;
}
.voice-name {
  font-weight: 600;
  font-size: 13px;
}
.voice-desc {
  font-size: 12px;
  color: var(--text-color-3);
  margin: 4px 0 6px;
  min-height: 16px;
}
.ml6 {
  margin-left: 6px;
}
.mt12 {
  margin-top: 12px;
}
.match-row {
  border: 1px solid var(--divider-color);
  border-radius: 6px;
  padding: 8px 10px;
  margin-top: 8px;
}
.match-main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.char-name {
  font-weight: 600;
}
.voice-name {
  color: var(--primary-color);
}
.score {
  font-size: 12px;
  color: var(--text-color-3);
}
.match-reason {
  font-size: 12px;
  color: var(--text-color-2);
  margin: 4px 0 6px;
}
.match-actions {
  display: flex;
  gap: 8px;
}
.alts {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-color-2);
}
.alts-title {
  color: var(--text-color-3);
}
.alt-item {
  margin-right: 12px;
}
</style>
