<script setup lang="ts">
/**
 * 资产库批量操作卡：工作流选择 / 全选 / 已选计数 / 批量生成。
 *
 * 改造点（2026-11-19 v7）：本卡原先自带一条「本批 X/Y + 进度条」，
 * 与上方进度卡重复表达进度。批量进度属于「执行进度」，已归入第一张卡；
 * 本卡只保留操作与其直接反馈（失败项重试、收起）。
 *
 * 工作流选择器固定 `asset_image` 槽位：候选由后端按真实能力过滤
 * （只允许无图片输入的纯文生图），角色/场景/道具共用同一份选择，
 * 避免出现角色用文生图、道具用图生图的混用。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import StageCard from '@/components/ui/StageCard.vue'
import WorkflowSelector from '@/components/workflow/WorkflowSelector.vue'
import type { BatchProgressView } from './types'

const props = withDefaults(
  defineProps<{
    /** 当前标签名（角色/场景/道具），用于「批量生成角色定妆图」这类文案 */
    scopeLabel?: string
    total: number
    selected: number
    batchLoading?: boolean
    inflightCount?: number
    batch?: BatchProgressView | null
    retrying?: boolean
    /** 当前标签下失败项数量 */
    failedCount?: number
    /** 当前选中的资产出图工作流（角色/场景/道具共用一份） */
    workflowId?: string
  }>(),
  {
    scopeLabel: '',
    batchLoading: false,
    inflightCount: 0,
    batch: null,
    retrying: false,
    failedCount: 0,
    workflowId: undefined,
  },
)

const emit = defineEmits<{
  'update:workflowId': [string | undefined]
  'toggle-all': [boolean]
  'batch-generate': []
  'retry-failed': []
  'clear-batch': []
}>()

const allChecked = computed(() => props.total > 0 && props.selected >= props.total)
const someChecked = computed(() => props.selected > 0 && !allChecked.value)
/** n-checkbox 的 indeterminate 不是响应式绑定，单独用 ref 同步 */
const indeterminate = ref(false)
watch(someChecked, (v) => {
  indeterminate.value = v
}, { immediate: true })
</script>

<template>
  <StageCard label="批量生成">
    <span class="wf-pick">
      <WorkflowSelector
        :model-value="workflowId"
        slot="asset_image"
        size="small"
        placeholder="选择出图工作流"
        @update:model-value="(v: string | undefined) => emit('update:workflowId', v)"
      />
    </span>
    <n-checkbox
      :checked="allChecked"
      :indeterminate="indeterminate"
      :disabled="!total"
      @update:checked="(v: boolean) => emit('toggle-all', v)"
    >
      全选
    </n-checkbox>
    <span class="sel-count">已选 <b>{{ selected }}</b> / {{ total }}</span>

    <template #actions>
      <span v-if="failedCount" class="failed">
        {{ failedCount }} 个生成失败
      </span>
      <n-button
        v-if="failedCount"
        size="small"
        quaternary
        type="warning"
        :loading="retrying"
        @click="emit('retry-failed')"
      >
        重试失败项
      </n-button>
      <n-button
        size="small"
        type="primary"
        :loading="batchLoading"
        :disabled="!selected"
        @click="emit('batch-generate')"
      >
        批量生成{{ scopeLabel }}（{{ selected }}）
      </n-button>
    </template>

    <template v-if="batch" #meta>
      <span class="batch-nums">
        本批 {{ batch.done }}/{{ batch.total }} · 生成中 {{ batch.running }}<template v-if="batch.failed"> · 失败 {{ batch.failed }}</template>
      </span>
      <n-button size="tiny" quaternary @click="emit('clear-batch')">收起本批</n-button>
    </template>
  </StageCard>
</template>

<style scoped>
.wf-pick {
  min-width: 170px;
  max-width: 260px;
  flex: 0 1 210px;
}
.sel-count {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.sel-count b {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-primary);
}
.failed {
  font-size: 12px;
  color: var(--state-danger);
}
.batch-nums {
  font-family: var(--font-mono);
  color: var(--text-secondary);
}

/* 窄屏让工作流选择器独占一行：768px 下其它控件已折行，
   选择器再挤在半行里会被压到 170px 下限以下，选项文字截断。 */
@media (max-width: 768px) {
  .wf-pick {
    flex: 1 1 100%;
    max-width: none;
  }
}
</style>
