<script setup lang="ts">
/**
 * 工作流选择器（P5）：展示名称/版本/能力标签/运行状态，能力全部来自
 * /capabilities（不按名称推断）。选择后把 workflow_id 交给父组件，父组件只传
 * 业务参数与素材引用，不接触任何 ComfyUI 节点信息。
 */
import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { NSelect, NSpin, NTag, NTooltip } from 'naive-ui'
import { getWorkflowCapabilities, listWorkflows } from '@/api/workflows'
import type { WorkflowCapability, WorkflowType } from '@/types/workflow'
import { MODE_LABELS } from '@/types/workflow'

const props = withDefaults(defineProps<{
  modelValue?: string
  workflowType?: WorkflowType
  disabled?: boolean
  placeholder?: string
  size?: 'small' | 'medium' | 'large'
}>(), {
  workflowType: 'image',
  placeholder: '选择工作流（缺省按能力自动匹配）',
  size: 'medium',
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | undefined): void
  (e: 'selected', workflow: { id: string; name: string; capabilities?: WorkflowCapability }): void
}>()

interface WorkflowRow {
  id: string
  name: string
  alias?: string | null
  version: number
  workflow_type: WorkflowType
  runtime_status?: string
  is_default?: boolean
  capabilities?: Record<string, unknown> | null
}

const listQuery = useQuery({
  queryKey: ['workflows', 'picker', props.workflowType],
  queryFn: () => listWorkflows({ workflow_type: props.workflowType, page_size: 50 }),
  staleTime: 60_000,
})

const rows = computed<WorkflowRow[]>(() => {
  const data: unknown = listQuery.data.value
  if (Array.isArray(data)) return data as WorkflowRow[]
  return ((data as { items?: WorkflowRow[] } | undefined)?.items ?? []) as WorkflowRow[]
})

const options = computed(() => rows.value.map((w) => {
  const tier = (w.capabilities as { tier?: string } | null | undefined)?.tier
  const flags: string[] = []
  if (w.is_default) flags.push('默认')
  if (tier) flags.push(tier)
  if (w.runtime_status && w.runtime_status !== 'ready' && w.runtime_status !== 'unchecked') {
    flags.push(w.runtime_status === 'unreachable' ? '实例不可达' : '缺依赖')
  }
  return {
    label: `${w.name}${flags.length ? ' · ' + flags.join(' / ') : ''} · v${w.version}`,
    value: w.id,
  }
}))

/** 选中项的能力：用于标签行展示；可能已随列表返回 meta 能力，仍需 /capabilities 归一化 */
const capability = ref<WorkflowCapability | null>(null)
const capabilityLoading = ref(false)

async function loadCapability(id: string | undefined) {
  capability.value = null
  if (!id) return
  capabilityLoading.value = true
  try {
    capability.value = await getWorkflowCapabilities(id)
  } catch {
    capability.value = null
  } finally {
    capabilityLoading.value = false
  }
}

watch(() => props.modelValue, (id) => { void loadCapability(id) }, { immediate: true })

function onChange(value: string | null) {
  const id = value ?? undefined
  emit('update:modelValue', id)
  const row = rows.value.find((w) => w.id === id)
  if (row) {
    emit('selected', { id: row.id, name: row.name, capabilities: capability.value ?? undefined })
  }
}
</script>

<template>
  <div class="wf-selector">
    <NSelect
      :value="modelValue ?? null"
      :options="options"
      :disabled="disabled || listQuery.isLoading.value"
      :placeholder="placeholder"
      :size="size"
      clearable
      filterable
      @update:value="onChange"
    />
    <NSpin v-if="capabilityLoading" :size="12" />
    <div v-if="capability" class="wf-tags">
      <NTag v-for="mode in capability.generation_modes" :key="mode" size="small" :bordered="false">
        {{ MODE_LABELS[mode] ?? mode }}
      </NTag>
      <NTooltip v-if="capability.reference.max_image_references">
        <template #trigger>
          <NTag size="small" :bordered="false">
            参考图 {{ capability.reference.max_image_references }}
          </NTag>
        </template>
        工作流声明的图片参考槽位上限
      </NTooltip>
      <NTag
        v-if="capability.video.supports_previous_tail_frame"
        size="small"
        :bordered="false"
      >
        上一镜尾帧
      </NTag>
      <NTag
        v-if="capability.audio.accepts_tts_reference_audio"
        size="small"
        :bordered="false"
      >
        TTS 音频参考 {{ capability.audio.max_audio_inputs }}
      </NTag>
      <NTag
        v-if="capability.modalities.video_output && capability.audio.supports_native_audio"
        size="small"
        :bordered="false"
      >
        原生音轨（{{ capability.audio.audio_output_mode ?? 'unknown' }}）
      </NTag>
    </div>
  </div>
</template>

<style scoped>
.wf-selector {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.wf-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
</style>
