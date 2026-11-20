<script setup lang="ts">
/**
 * 统一工作流选择器。
 *
 * 两条取数路径：
 * 1. 传 `slot`（asset_image / storyboard_image / video）：走后端 `/workflows/candidates`，
 *    候选与否完全由后端按真实能力解析得出（纯文生图 / 图生图 / H3 多参考图视频），
 *    前端只负责展示与禁用，不在这里二次推断能力，避免各页面各写一套规则后漂移。
 * 2. 不传 `slot`（管理、测试场景）：退回按 workflow_type 列全部工作流。
 *
 * 展示规则：主文本只用 `description`（用户在工作流管理页维护），技术名只作副标题提示，
 * 不让用户去理解 ComfyUI 工作流内部名称；描述被修改后通过 queryClient 失效候选缓存刷新。
 */
import { computed, h, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { NAlert, NSelect, NSpin, NTag, NTooltip } from 'naive-ui'
import {
  getWorkflowCapabilities,
  listWorkflowCandidates,
  listWorkflows,
} from '@/api/workflows'
import type {
  WorkflowCandidateItem,
  WorkflowCapability,
  WorkflowSlotKey,
  WorkflowType,
} from '@/types/workflow'
import { MODE_LABELS } from '@/types/workflow'

const props = withDefaults(defineProps<{
  modelValue?: string
  /** 业务槽位：传了就走按能力过滤的候选接口 */
  slot?: WorkflowSlotKey
  /** 未传 slot 时的类型过滤 */
  workflowType?: WorkflowType
  disabled?: boolean
  placeholder?: string
  size?: 'small' | 'medium' | 'large'
  /** 选中项下方是否展示能力标签行 */
  showTags?: boolean
  /** 未选择时自动落到默认工作流（无默认时取第一个可用项） */
  autoDefault?: boolean
}>(), {
  placeholder: '选择工作流',
  size: 'small',
  showTags: false,
  autoDefault: true,
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | undefined): void
  (e: 'selected', workflow: {
    id: string
    name: string
    description: string
    slot?: string
    capabilities?: WorkflowCapability
  }): void
}>()

interface Row extends WorkflowCandidateItem {
  /** 内部技术名，仅用于副标题提示 */
  internalName: string
}

const candidatesQuery = useQuery({
  queryKey: computed(() => ['workflow-candidates', props.slot] as const),
  queryFn: () => listWorkflowCandidates(props.slot as WorkflowSlotKey),
  enabled: computed(() => Boolean(props.slot)),
  staleTime: 30_000,
})

const listQuery = useQuery({
  queryKey: computed(() => ['workflows', 'picker', props.workflowType] as const),
  queryFn: () => listWorkflows({ workflow_type: props.workflowType, page_size: 50 }),
  enabled: computed(() => !props.slot),
  staleTime: 60_000,
})

const loading = computed(() =>
  props.slot ? candidatesQuery.isLoading.value : listQuery.isLoading.value,
)

const loadError = computed(() => {
  const e = props.slot ? candidatesQuery.error.value : listQuery.error.value
  if (!e) return ''
  return e instanceof Error ? e.message : String(e)
})

/** 无 slot 时把列表结果补齐成候选形状（不做任何能力推断） */
const rows = computed<Row[]>(() => {
  if (props.slot) {
    return (candidatesQuery.data.value?.items ?? []).map((r) => ({ ...r, internalName: r.name }))
  }
  const data: unknown = listQuery.data.value
  const items = Array.isArray(data)
    ? (data as Array<Record<string, unknown>>)
    : ((data as { items?: Array<Record<string, unknown>> } | undefined)?.items ?? [])
  return items.map((w) => {
    const caps = (w.capabilities ?? {}) as Record<string, unknown>
    const name = String(w.name ?? '')
    return {
      id: String(w.id),
      name,
      internalName: name,
      description: String(w.description ?? name),
      workflow_type: (w.workflow_type ?? 'image') as WorkflowType,
      version: Number(w.version ?? 0),
      is_default: Boolean(w.is_default),
      is_builtin: Boolean(w.is_builtin),
      runtime_status: (w.runtime_status as string | null | undefined) ?? null,
      generation_modes: (caps.generation_modes as string[] | undefined) ?? [],
      usable: true,
      missing: [],
    }
  })
})

const usableRows = computed(() => rows.value.filter((r) => r.usable))

/**
 * 真正可选的行：能力匹配 **且**（业务槽位下）不缺依赖。
 *
 * 两种「不可用」要分开对待：
 * - `usable=false`（能力不匹配，如资产库要求纯文生图却声明了图片输入）：工作流本身
 *   干不了这件事，永远不该出现在下拉里。
 * - `runtime_status='missing_deps'`（实例缺节点/模型）：业务页真跑不起来，不展示。
 * - `runtime_status='unreachable'`（实例连不上）：这是 ComfyUI 实例/隧道的环境问题，
 *   不代表工作流有缺陷——隧道域名会轮换，隐藏它会让整个槽位空掉、用户连选都选不了。
 *   因此保留可选，仅在副文本标注「实例不可达」。
 * - `unchecked` / `ready`：保留。
 *
 * 但**不传 slot** 的场景（设置页「工作流测试与版本」）必须保留全部工作流：
 * 那里正是用来对缺依赖的工作流做依赖检查/测试提交的，隐藏掉等于没法修。
 */
const selectableRows = computed(() =>
  props.slot
    ? usableRows.value.filter((r) => r.runtime_status !== 'missing_deps')
    : usableRows.value,
)

/**
 * 默认项优先级：槽位推荐项 → 后端 is_default → 第一个可选行。
 * 推荐项由后端按业务槽位显式声明（资产库纯文生图 / 分镜图多参考图生图 /
 * 分镜看板多参考图生视频），不依赖列表排序，工作流新增或改名也不会漂移。
 */
const defaultRow = computed<Row | undefined>(
  () =>
    selectableRows.value.find((r) => r.is_recommended) ??
    selectableRows.value.find((r) => r.is_default) ??
    selectableRows.value[0],
)

const currentRow = computed<Row | undefined>(
  () => rows.value.find((r) => r.id === props.modelValue),
)

/** 选中值是否还在下拉里；不在就回 null，避免 n-select 直接显示原始 ID */
const selectValue = computed(() =>
  selectableRows.value.some((r) => r.id === props.modelValue) ? props.modelValue ?? null : null,
)

/** 当前选中项失效或不再满足该业务能力：明确提示，不静默改选 */
const invalidMessage = computed(() => {
  if (!props.modelValue || loading.value) return ''
  const row = currentRow.value
  if (!row) return '当前选择的工作流已不存在，请重新选择'
  if (!row.usable) {
    const why = row.missing.length ? `：${row.missing.join('、')}` : ''
    return `当前工作流不满足该业务要求${why}，请改选其他工作流`
  }
  if (props.slot && row.runtime_status === 'missing_deps') {
    return '当前工作流在 ComfyUI 实例上缺节点或模型，已从可选项中移除，请改选其他工作流'
  }
  return ''
})

const noUsableMessage = computed(() => {
  if (loading.value || loadError.value) return ''
  if (selectableRows.value.length) return ''
  if (!rows.value.length) return '还没有可用的工作流，请先到「设置 → 工作流」导入'
  return '当前没有满足该业务能力要求的工作流，请到「设置 → 工作流」补充'
})

/**
 * 下拉项：只列可用工作流，且只显示名称。
 *
 * 副标题行（「文生图 · 图生视频 · 实例不可达」「推荐」「默认」）已删除：
 * 选择器的职责就是「选哪个工作流」，能力标签与运行时状态属于设置页的检查结果，
 * 摆在选项里只是把每一行撑成两行、让下拉变成一片灰字噪声。
 * 运行时不可用的行本来就已过滤掉（见 selectableRows），选中值失效另有 invalidMessage 提示。
 */
const options = computed(() => selectableRows.value.map((row) => ({
  value: row.id,
  label: row.description || row.internalName,
  row,
})))

/** 主文本 = 工作流描述；不再渲染副标题行 */
function renderLabel(option: { label?: string }) {
  return h('span', String(option.label ?? ''))
}

/** 选中项能力标签（仅 showTags 时按需拉一次 /capabilities 补充展示） */
const capability = ref<WorkflowCapability | null>(null)
const capabilityLoading = ref(false)

async function loadCapability(id: string | undefined) {
  capability.value = null
  if (!id || !props.showTags) return
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

/**
 * 自动落到默认工作流：父组件没给值、或给的值已不在可选列表里（能力不匹配 / 缺依赖）
 * 时补一次默认值。否则页面会保留一个下拉里看不到、批量生成时必然失败的 ID。
 * 仅当存在可选默认项时才改，绝不把用户选中的有效项换掉。
 */
watch(
  () => [props.modelValue, defaultRow.value?.id, loading.value, selectableRows.value.length] as const,
  ([current, def, isLoading]) => {
    if (!props.autoDefault || isLoading || !def) return
    const currentSelectable = current && selectableRows.value.some((r) => r.id === current)
    if (currentSelectable) return
    if (current === def) return
    emit('update:modelValue', def)
  },
  { immediate: true },
)

function onChange(value: string | null) {
  const id = value ?? undefined
  emit('update:modelValue', id)
  const row = rows.value.find((r) => r.id === id)
  if (row) {
    emit('selected', {
      id: row.id,
      name: row.internalName,
      description: row.description,
      slot: props.slot,
      capabilities: capability.value ?? undefined,
    })
  }
}
</script>

<template>
  <div class="wf-selector">
    <n-spin v-if="loading" :size="12" />
    <n-select
      v-else
      :value="selectValue"
      :options="options"
      :disabled="disabled"
      :placeholder="placeholder"
      :size="size"
      :render-label="renderLabel"
      clearable
      filterable
      @update:value="onChange"
    />
    <n-alert v-if="noUsableMessage" type="warning" :bordered="false" class="wf-alert">
      {{ noUsableMessage }}
    </n-alert>
    <n-alert v-else-if="loadError" type="error" :bordered="false" class="wf-alert">
      工作流加载失败：{{ loadError }}
    </n-alert>
    <n-alert v-else-if="invalidMessage" type="warning" :bordered="false" class="wf-alert">
      {{ invalidMessage }}
    </n-alert>
    <div v-if="showTags && capability" class="wf-tags">
      <n-tag v-for="mode in capability.generation_modes" :key="mode" size="small" :bordered="false">
        {{ MODE_LABELS[mode] ?? mode }}
      </n-tag>
      <n-tooltip v-if="capability.reference.max_image_references">
        <template #trigger>
          <n-tag size="small" :bordered="false">
            参考图 {{ capability.reference.max_image_references }}
          </n-tag>
        </template>
        工作流声明的图片参考槽位上限
      </n-tooltip>
    </div>
  </div>
</template>

<style scoped>
.wf-selector {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.wf-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.wf-alert {
  padding: 4px 8px;
  font-size: 12px;
}
</style>
