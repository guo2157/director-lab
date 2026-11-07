<script setup lang="ts">
import { computed } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { NCard, NRadio, NRadioGroup, NSpin, NSwitch, NTag, useMessage } from 'naive-ui'
import { getAudioConfig, setAudioConfig } from '@/api/audio'
import { NATIVE_AUDIO_POLICY_OPTIONS } from '@/types/audio'
import type { AudioConfig, NativeAudioPolicy } from '@/types/audio'

const props = defineProps<{
  projectId: string
}>()

const message = useMessage()
const queryClient = useQueryClient()

const configQuery = useQuery({
  queryKey: computed(() => ['audio-config', props.projectId]),
  queryFn: () => getAudioConfig(props.projectId),
  enabled: computed(() => !!props.projectId),
})

const config = computed<AudioConfig | null>(() => configQuery.data.value?.audio_config ?? null)

/** 当前策略说明文案 */
const policyHint = computed(() => {
  const cur = config.value?.native_audio_policy
  return NATIVE_AUDIO_POLICY_OPTIONS.find((o) => o.value === cur)?.description ?? ''
})

async function patch(partial: Partial<AudioConfig>) {
  try {
    await setAudioConfig(props.projectId, partial)
    await queryClient.invalidateQueries({ queryKey: ['audio-config', props.projectId] })
    message.success('音频策略已保存')
  } catch (e) {
    message.error(`保存失败：${(e as Error).message}`)
  }
}

function onPolicyChange(v: NativeAudioPolicy) {
  patch({ native_audio_policy: v })
}
</script>

<template>
  <NCard size="small" title="音频策略" class="audio-policy-panel">
    <template #header-extra>
      <NTag v-if="config" size="small" :bordered="false" type="info">
        {{ config.tts_provider }} / {{ config.tts_model }}
      </NTag>
    </template>
    <NSpin v-if="configQuery.isLoading.value" size="small" />
    <div v-else-if="!config" class="policy-empty">音频策略加载失败，请刷新重试</div>
    <div v-else class="policy-body">
      <div class="policy-row">
        <span class="policy-label">启用 TTS 对白/旁白</span>
        <NSwitch
          :value="config.tts_enabled"
          size="small"
          @update:value="(v: boolean) => patch({ tts_enabled: v })"
        />
      </div>
      <div class="policy-row">
        <span class="policy-label">H3 原生音轨处理</span>
      </div>
      <NRadioGroup
        :value="config.native_audio_policy"
        size="small"
        class="policy-radios"
        @update:value="onPolicyChange"
      >
        <NRadio
          v-for="opt in NATIVE_AUDIO_POLICY_OPTIONS"
          :key="opt.value"
          :value="opt.value"
          :label="opt.label"
        />
      </NRadioGroup>
      <div v-if="policyHint" class="policy-hint">{{ policyHint }}</div>
      <div class="policy-row">
        <span class="policy-label">混入 BGM</span>
        <NSwitch
          :value="config.bgm_enabled"
          size="small"
          @update:value="(v: boolean) => patch({ bgm_enabled: v })"
        />
      </div>
      <div class="policy-row">
        <span class="policy-label">混入 SFX / 环境声</span>
        <NSwitch
          :value="config.sfx_enabled"
          size="small"
          @update:value="(v: boolean) => patch({ sfx_enabled: v })"
        />
      </div>
    </div>
  </NCard>
</template>

<style scoped>
.audio-policy-panel { margin-top: 12px; }
.policy-body { display: flex; flex-direction: column; gap: 10px; }
.policy-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.policy-label { font-size: 13px; font-weight: 600; }
.policy-radios { display: flex; flex-direction: column; gap: 4px; }
.policy-hint { color: rgba(128, 128, 128, 0.95); font-size: 12px; line-height: 1.5; padding-left: 2px; }
.policy-empty { color: rgba(128, 128, 128, 0.9); font-size: 12px; padding: 8px 0; }
</style>
