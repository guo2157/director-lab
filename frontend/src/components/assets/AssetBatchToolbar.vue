<script setup lang="ts">
/**
 * 资产库批量操作卡：工作流选择 / 全选 / 已选计数 / 批量生成。
 *
 * 改造点（2026-11-19 v7）：本卡原先自带一条「本批 X/Y + 进度条」，
 * 与上方进度卡重复表达进度。批量进度属于「执行进度」，已归入第一张卡；
 * 本卡只保留操作与其直接反馈（失败项重试）。
 *
 * 2026-11-21：再删除选择器下方那行「本批 X/Y · 生成中 N」与「收起本批」按钮。
 * 本卡不再渲染 #meta 第二行，批次统计只保留在上方进度卡。
 *
 * 工作流选择器固定 `asset_image` 槽位 + `image_text_to_image` 模型类型：
 * 资产库定妆图只能是文生图模型，模型类型不需要用户选，页面上不展示该下拉，
 * 只在选择器上方以标签提示「文生图模型」。角色/场景/道具共用同一份选择。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import StageCard from '@/components/ui/StageCard.vue'
import WorkflowSelector from '@/components/workflow/WorkflowSelector.vue'

const props = withDefaults(
  defineProps<{
    /** 当前标签名（角色/场景/道具），用于「批量生成角色定妆图」这类文案 */
    scopeLabel?: string
    total: number
    selected: number
    batchLoading?: boolean
    inflightCount?: number
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
        fixed-model-type="image_text_to_image"
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
    <span class="stage-sel">已选 <b>{{ selected }}</b> / {{ total }}</span>

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

  </StageCard>
</template>

<style scoped>
.wf-pick {
  min-width: 170px;
  max-width: 260px;
  flex: 0 1 210px;
}
/* 「已选 X / Y」用全局 .stage-sel（三页共用一份口径）。 */
/* 失败项：红色文字 + 「重试失败项」次按钮成组出现，
   与主按钮之间由 StageCard 的 actions gap 隔开。 */
.failed {
  font-size: 13px;
  color: var(--danger-text);
}

/* 窄屏：工作流选择器不再独占整行（独占会把卡片撑到 180px），
   改为与全选/计数同排的弹性项；宽度下限降到 130px 以适配 390 视口。 */
@media (max-width: 768px) {
  .wf-pick {
    flex: 1 1 130px;
    min-width: 130px;
    max-width: none;
  }
}
</style>
