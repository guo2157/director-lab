<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, NDrawer, NDrawerContent, NInput, NProgress, NSelect, NSpin, useMessage } from 'naive-ui'
import {
  createCharacterVoice,
  getAudioAssetUrl,
  getVoicePreviewCache,
  listCharacterVoices,
  listSpeakers,
  previewVoice,
} from '@/api/audio'
import { getJob } from '@/api/jobs'
import type { VoiceProfile } from '@/types/audio'

const props = defineProps<{
  show: boolean
  characterId: string | null
  characterName?: string
}>()

const emit = defineEmits<{ (e: 'update:show', v: boolean): void }>()

const message = useMessage()
const queryClient = useQueryClient()

const visible = computed({
  get: () => props.show,
  set: (v: boolean) => emit('update:show', v),
})

// speaker 列表（来自后端 workflow meta，禁止前端硬编码）
const speakersQuery = useQuery({
  queryKey: ['audio-speakers'],
  queryFn: () => listSpeakers(),
  enabled: computed(() => visible.value),
})

/** speaker 网格卡片（选中态由 formSpeaker 驱动） */
const speakerCards = computed(() =>
  (speakersQuery.data.value?.speakers ?? []).map((s) => ({
    value: s.speaker,
    label: s.display_name || s.speaker,
    /** 英文 speaker 标识作为副标题，便于与工作流/文档对照 */
    sub: s.display_name && s.display_name !== s.speaker ? s.speaker : '',
    language: (s.language ?? []).join(' / '),
    /** 性别标签：男声 / 女声（来自工作流 voice_genders） */
    gender: s.gender === 'male' ? '男声' : s.gender === 'female' ? '女声' : '',
    /** 音色特点：取 description 中竖线之后的短句，避免与性别标签重复 */
    desc: (s.description || '').split('|').slice(1).join('|').trim(),
  })),
)

// 角色音色（单条覆盖语义：该角色恒为 1 条配置）
const voicesQuery = useQuery({
  queryKey: computed(() => ['character-voices', props.characterId]),
  queryFn: () => listCharacterVoices(props.characterId as string),
  enabled: computed(() => visible.value && !!props.characterId),
})

const current = computed<VoiceProfile | null>(
  () => (voicesQuery.data.value?.voices ?? [])[0] ?? null,
)
const hasVoice = computed(() => !!current.value)

/** 横幅 avatar 首字（角色名优先，其次音色名） */
const heroInitial = computed(() => {
  const src = props.characterName || current.value?.voice_name || current.value?.voice_id || '音'
  return src.trim().slice(0, 1)
})

// 表单（单份，保存即整体覆盖）
const formSpeaker = ref<string | null>(null)
const formInstruct = ref('')
/** 语言下拉：取值与 TTS 工作流 language 入参一致（Chinese / English），不再手工填写 */
const LANGUAGE_OPTIONS = [
  { label: '中文', value: 'Chinese' },
  { label: '英文', value: 'English' },
]
const formLanguage = ref('Chinese')
/** speaker 英文标识 → 中文显示名（未知则原样返回） */
function speakerName(id?: string | null) {
  if (!id) return ''
  const hit = speakerCards.value.find((s) => s.value === id)
  return hit?.label || id
}
/** speaker 英文标识 → 性别标签（男声/女声），未知返回空 */
function speakerGender(id?: string | null) {
  if (!id) return ''
  const hit = speakerCards.value.find((s) => s.value === id)
  return hit?.gender || ''
}
/** speaker 英文标识 → 音色特点短句 */
function speakerDesc(id?: string | null) {
  if (!id) return ''
  const hit = speakerCards.value.find((s) => s.value === id)
  return hit?.desc || ''
}
/** 语言取值 → 中文标签 */
function languageLabel(v?: string | null) {
  if (!v) return '未设置语言'
  const hit = LANGUAGE_OPTIONS.find((o) => o.value === v)
  return hit?.label || v
}

/** 配置到达或切换角色时回填：打开抽屉看到的就是当前生效值 */
watch(
  current,
  (v) => {
    formSpeaker.value = v?.voice_id ?? null
    formInstruct.value = v?.default_instruct ?? ''
    // 历史数据可能存 Auto 等非下拉取值，统一归一到下拉可选项，避免下拉显示空白
    formLanguage.value = v?.language === 'English' ? 'English' : 'Chinese'
  },
  { immediate: true },
)

const saveMutation = useMutation({
  mutationFn: () =>
    createCharacterVoice(props.characterId as string, {
      voice_id: formSpeaker.value as string,
      default_instruct: formInstruct.value,
      language: formLanguage.value,
    }),
  onSuccess: () => {
    message.success(hasVoice.value ? '音色已更新（覆盖原配置）' : '音色已设置')
    queryClient.invalidateQueries({ queryKey: ['character-voices', props.characterId] })
  },
  onError: (e: Error) => message.error(`保存失败：${e.message}`),
})

/** 试听任务状态：在抽屉内就地展示进度与结果，无需跳转任务中心 */
const previewJobId = ref<string | null>(null)
const previewStatus = ref<'' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cached'>('')
const previewProgress = ref(0)
const previewError = ref('')
const previewAudioUrl = ref('')
const previewing = computed(
  () => previewStatus.value === 'queued' || previewStatus.value === 'running',
)
const PREVIEW_STATUS_TEXT: Record<string, string> = {
  queued: '试听排队中',
  running: '试听合成中',
  succeeded: '试听生成完成',
  failed: '试听生成失败',
  cached: '已使用缓存的试听音频',
}
/** 当次结果是否来自缓存（不消耗 ComfyUI） */
const previewFromCache = ref(false)
let previewTimer: ReturnType<typeof setInterval> | null = null
function stopPreviewPoll() {
  if (previewTimer) {
    clearInterval(previewTimer)
    previewTimer = null
  }
}
function resetPreview() {
  stopPreviewPoll()
  previewJobId.value = null
  previewStatus.value = ''
  previewProgress.value = 0
  previewError.value = ''
  previewAudioUrl.value = ''
  previewFromCache.value = false
}
/** 轮询试听任务；成功后取出音频资产地址就地播放 */
async function pollPreview(jobId: string) {
  try {
    const job = await getJob(jobId)
    if (previewJobId.value !== jobId) return
    const st = job.status as string
    if (st === 'succeeded') {
      stopPreviewPoll()
      previewStatus.value = 'succeeded'
      previewProgress.value = 100
      const assets =
        ((job.result as { assets?: Array<{ asset_id?: string; asset_type?: string }> } | null)
          ?.assets) ?? []
      const audio = assets.find((a) => a.asset_type === 'audio') ?? assets[0]
      if (audio?.asset_id) previewAudioUrl.value = getAudioAssetUrl(audio.asset_id)
      return
    }
    if (st === 'failed' || st === 'lost' || st === 'canceled') {
      stopPreviewPoll()
      previewStatus.value = 'failed'
      previewError.value = job.error_msg || job.error_code || '任务未完成，请重试'
      return
    }
    previewStatus.value = st === 'running' ? 'running' : 'queued'
    previewProgress.value = job.progress ?? 0
  } catch (e) {
    stopPreviewPoll()
    previewStatus.value = 'failed'
    previewError.value = (e as Error)?.message || '查询试听任务状态失败'
  }
}
/**
 * 请求试听
 * - force=false：优先命中后端缓存，speaker/语言/情绪指令/文本未变时直接拿到音频，不调用 ComfyUI
 * - force=true ：跳过缓存重新合成，成功后覆盖缓存（对应「重新生成」按钮）
 */
function previewCurrent(force = false) {
  const v = current.value
  if (!v) {
    message.warning('请先保存音色后再试听')
    return
  }
  resetPreview()
  previewVoice(v.id, { force })
    .then((r) => {
      if (r.cached) {
        // 命中缓存：直接展示音频，进度区不出现，无需轮询
        previewFromCache.value = true
        previewStatus.value = 'cached'
        previewAudioUrl.value = r.asset_id ? getAudioAssetUrl(r.asset_id) : (r.audio_url ?? '')
        return
      }
      if (!r.job_id) {
        previewStatus.value = 'failed'
        previewError.value = '未返回任务 ID'
        return
      }
      // 先落局部常量：箭头函数闭包里 TS 不再保留 r.job_id 的类型窄化
      const jobId = r.job_id
      previewJobId.value = jobId
      previewStatus.value = 'queued'
      void pollPreview(jobId)
      previewTimer = setInterval(() => void pollPreview(jobId), 1500)
    })
    .catch((e: Error) => {
      previewStatus.value = 'failed'
      previewError.value = e.message
    })
}
/**
 * 抽屉打开时只读探测缓存：命中直接播放，未命中不发起任何合成。
 * 修复「进入页面就重新生成一遍」——打开动作绝不能产生算力消耗，
 * 只有用户点击「试听」或「重新生成」才会真正提交合成任务。
 */
async function loadPreviewCache() {
  const v = current.value
  if (!v) return
  resetPreview()
  try {
    const r = await getVoicePreviewCache(v.id)
    if (!r.cached || !r.asset_id) return
    previewFromCache.value = true
    previewStatus.value = 'cached'
    previewAudioUrl.value = getAudioAssetUrl(r.asset_id)
  } catch {
    // 探测失败静默忽略，不阻断用户手动试听
  }
}
/** 抽屉打开或音色变化时只做缓存探测（只读），不触发合成 */
watch(
  () => [visible.value, current.value?.id, current.value?.voice_id, current.value?.language, current.value?.default_instruct],
  () => {
    if (!visible.value || !current.value) return
    if (previewing.value) return
    void loadPreviewCache()
  },
  { immediate: true },
)
onBeforeUnmount(stopPreviewPoll)
</script>

<template>
  <NDrawer v-model:show="visible" :width="'min(560px, 100vw)'" placement="right">
    <NDrawerContent closable>
      <template #header>
        <div class="drawer-head">
          <span class="drawer-title">音色配置</span>
          <span v-if="props.characterName" class="drawer-char">{{ props.characterName }}</span>
        </div>
      </template>

      <NSpin v-if="voicesQuery.isLoading.value" size="small" />
      <template v-else>
        <!-- 当前音色横幅 -->
        <div v-if="current" class="hero">
          <div class="hero-avatar">{{ heroInitial }}</div>
          <div class="hero-main">
            <div class="hero-cap">当前音色</div>
            <div class="hero-name">{{ speakerName(current.voice_name || current.voice_id) }}</div>
            <div class="hero-sub">
              <span>{{ languageLabel(current.language) }}</span>
              <span class="dot">·</span>
              <span>{{ current.default_instruct || '未设置情绪指令' }}</span>
            </div>
            <div
              v-if="speakerGender(current.voice_id) || speakerDesc(current.voice_id)"
              class="hero-voice"
            >
              <span
                v-if="speakerGender(current.voice_id)"
                class="sp-gender"
                :class="speakerGender(current.voice_id) === '女声' ? 'is-female' : 'is-male'"
              >{{ speakerGender(current.voice_id) }}</span>
              <span v-if="speakerDesc(current.voice_id)" class="hero-voice-desc">
                {{ speakerDesc(current.voice_id) }}
              </span>
            </div>
          </div>
          <div class="hero-actions">
            <NButton
              size="small"
              type="primary"
              ghost
              :loading="previewing"
              :disabled="previewing"
              @click="previewCurrent(false)"
            >{{ previewing ? '合成中' : '试听' }}</NButton>
            <NButton
              size="tiny"
              quaternary
              :loading="previewing"
              :disabled="previewing"
              @click="previewCurrent(true)"
            >重新生成</NButton>
          </div>
        </div>
        <div v-else class="hero hero-empty">
          <div class="hero-avatar muted">?</div>
          <div class="hero-main">
            <div class="hero-cap">尚未设置音色</div>
            <div class="hero-sub">选择 Speaker 后保存即可，保存后立即生效</div>
          </div>
        </div>

        <!-- 试听进度：就地展示排队/合成/完成状态，完成后直接播放 -->
        <div
          v-if="previewStatus"
          class="preview-box"
          :class="{ 'is-error': previewStatus === 'failed', 'is-cached': previewFromCache }"
        >
          <div class="preview-head">
            <span class="preview-title">{{ PREVIEW_STATUS_TEXT[previewStatus] || previewStatus }}</span>
            <span v-if="previewJobId" class="preview-job">{{ previewJobId }}</span>
            <span v-else-if="previewFromCache" class="preview-job">不消耗合成资源</span>
          </div>
          <NProgress
            v-if="previewing"
            type="line"
            :percentage="previewProgress"
            :height="6"
            processing
          />
          <p v-if="previewStatus === 'failed'" class="preview-err">
            {{ previewError || '任务未完成' }}
          </p>
          <audio v-if="previewAudioUrl" class="preview-audio" :src="previewAudioUrl" controls />
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title">选择 Speaker</span>
            <span class="panel-hint">来自模型支持列表</span>
          </div>
          <NSpin v-if="speakersQuery.isLoading.value" size="small" />
          <div v-else class="speaker-grid">
            <button
              v-for="s in speakerCards"
              :key="s.value"
              type="button"
              class="speaker-card"
              :class="{ active: formSpeaker === s.value }"
              @click="formSpeaker = s.value"
            >
              <span class="sp-name">
                {{ s.label }}
                <span
                  v-if="s.gender"
                  class="sp-gender"
                  :class="s.gender === '女声' ? 'is-female' : 'is-male'"
                >{{ s.gender }}</span>
              </span>
              <span v-if="s.sub" class="sp-lang">{{ s.sub }}</span>
              <span v-else-if="s.language" class="sp-lang">{{ s.language }}</span>
              <span v-if="s.desc" class="sp-desc">{{ s.desc }}</span>
            </button>
          </div>

          <div class="field">
            <span class="field-label">默认情绪指令</span>
            <NInput v-model:value="formInstruct" size="small" placeholder="如：自然、清晰、成熟" />
          </div>
          <div class="field">
            <span class="field-label">语言</span>
            <NSelect v-model:value="formLanguage" size="small" :options="LANGUAGE_OPTIONS" />
          </div>

          <NButton
            type="primary"
            block
            :disabled="!formSpeaker"
            :loading="saveMutation.isPending.value"
            @click="saveMutation.mutate()"
          >
            {{ !formSpeaker
              ? '请先选择 Speaker'
              : (hasVoice
                ? `保存音色（覆盖 ${speakerName(current?.voice_id)}）`
                : `保存音色（${speakerName(formSpeaker)}）`) }}
          </NButton>

          <p class="tip">音色只保留一份，保存即覆盖原配置，不保留历史版本；已生成的历史音频不受影响。</p>
        </div>
      </template>
    </NDrawerContent>
  </NDrawer>
</template>

<style scoped>
.drawer-head { display: flex; align-items: baseline; gap: 10px; }
.drawer-title { font-size: 16px; font-weight: 700; }
.drawer-char {
  font-size: 13px; font-weight: 500; padding: 1px 8px; border-radius: var(--radius-sm);
  color: var(--brand-primary);
  background: color-mix(in srgb, var(--brand-primary) 12%, transparent);
}

/* 当前音色横幅 */
.hero {
  display: flex; align-items: center; gap: 14px;
  padding: 16px 18px; border-radius: 14px; margin-bottom: 16px;
  background: color-mix(in srgb, var(--brand-primary) 7%, transparent);
  border: 1px solid color-mix(in srgb, var(--brand-primary) 26%, transparent);
}
.hero-empty { background: rgba(128, 128, 128, 0.06); border-style: dashed; border-color: rgba(128, 128, 128, 0.25); }
.hero-avatar {
  width: 52px; height: 52px; flex: 0 0 52px; border-radius: var(--radius-lg);
  display: flex; align-items: center; justify-content: center;
  font-size: 22px; font-weight: 700; color: var(--text-on-brand);
  background: var(--brand-primary);
  box-shadow: none;
}
.hero-avatar.muted { background: rgba(128, 128, 128, 0.35); color: var(--text-primary); }
.hero-main { flex: 1; min-width: 0; }
.hero-cap { font-size: 12px; color: var(--text-secondary); }
.hero-name { font-size: 20px; font-weight: 700; line-height: 1.3; }
.hero-sub { font-size: 12px; opacity: 0.8; margin-top: 2px; }
.hero-actions { flex: 0 0 auto; }
.dot { margin: 0 6px; opacity: 0.6; }

/* 表单面板 */
.panel {
  border: 1px solid rgba(128, 128, 128, 0.16); border-radius: 14px;
  padding: 14px; display: flex; flex-direction: column; gap: 12px;
  background: rgba(128, 128, 128, 0.02);
}
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.panel-title { font-size: 13px; font-weight: 700; }
.panel-hint { font-size: 11px; opacity: 0.66; }

/* Speaker 网格卡片 */
.speaker-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.speaker-card {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  padding: 9px 10px; border-radius: 10px; cursor: pointer; text-align: left;
  border: 1.5px solid rgba(128, 128, 128, 0.2);
  background: transparent; transition: all 0.16s ease;
}
.speaker-card:hover {
  transform: translateY(-1px);
  border-color: color-mix(in srgb, var(--brand-primary) 45%, transparent);
  background: color-mix(in srgb, var(--brand-primary) 6%, transparent);
}
.speaker-card.active {
  border-color: var(--brand-primary);
  background: color-mix(in srgb, var(--brand-primary) 14%, transparent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand-primary) 16%, transparent);
}
.sp-name { font-size: 13px; font-weight: 600; }
.sp-lang { font-size: 11px; opacity: 0.62; }
/* 性别标签：女声粉、男声蓝，与音色特点同行展示 */
.sp-gender {
  display: inline-block; margin-left: 5px; padding: 0 5px; border-radius: 999px;
  font-size: 10px; font-weight: 600; line-height: 15px; vertical-align: 1px;
}
.sp-gender.is-female { color: #db2777; background: rgba(219, 39, 119, 0.12); }
.sp-gender.is-male { color: #2563eb; background: rgba(37, 99, 235, 0.12); }
.sp-desc {
  font-size: 10px; line-height: 1.35; opacity: 0.66;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.hero-voice { display: flex; align-items: center; gap: 4px; margin-top: 4px; font-size: 11px; }
.hero-voice-desc { opacity: 0.75; }

/* 试听进度区 */
.preview-box {
  border: 1px solid rgba(128, 128, 128, 0.18); border-radius: 12px;
  padding: 10px 12px; margin-bottom: 16px;
  display: flex; flex-direction: column; gap: 8px;
  background: rgba(128, 128, 128, 0.03);
}
.preview-box.is-error { border-color: rgba(239, 68, 68, 0.4); background: rgba(239, 68, 68, 0.05); }
/* 缓存命中态：绿色描边区分「已生成过」与「刚合成」 */
.preview-box.is-cached { border-color: rgba(34, 197, 94, 0.35); background: rgba(34, 197, 94, 0.05); }
.hero-actions { display: flex; align-items: center; gap: 6px; }
.preview-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.preview-title { font-size: 12px; font-weight: 700; }
.preview-job { font-size: 11px; opacity: 0.55; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.preview-err { margin: 0; font-size: 11px; line-height: 1.5; color: #ef4444; }
.preview-audio { width: 100%; height: 32px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field-label { font-size: 12px; font-weight: 600; opacity: 0.82; }

.tip { margin: 0; font-size: 11px; line-height: 1.5; opacity: 0.62; }
</style>
