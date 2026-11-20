<script setup lang="ts">
/**
 * 分镜图批量操作卡：工作流选择 / 全选 / 批量生成。
 *
 * 模型类型固定为图生图（参考图生图）：分镜图必须接角色 / 场景 / 道具参考图，
 * 因此不展示模型类型下拉，只在选择器上方以标签提示「图生图模型」。
 *
 * 批量审核通过已删除（2026-11 流程简化）：逐镜人工确认闸口取消，
 * 阶段推进改为产物就绪判定，不再需要批量写确认标记。
 *
 * 改造点（2026-11-19 v8）：删掉状态筛选 chips（全部 / 已完成 / 待确认 / 失败）。
 * 那四个数字回答的是「各状态各有多少张」——而进度卡上的 X/Y 与进度条已经回答了
 * 「做到哪了」，镜头卡片右上角也各自带着状态标签。筛选条把同一批计数在第三个位置
 * 又列一遍，还额外占了一整行。需要找特定状态的镜头时用卡片标签扫读即可。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import StageCard from '@/components/ui/StageCard.vue'
import WorkflowSelector from '@/components/workflow/WorkflowSelector.vue'

const props = withDefaults(
  defineProps<{
    total: number
    selected: number
    allSelected?: boolean
    batchLoading?: boolean
    inflightCount?: number
    /** 当前选中的分镜出图工作流（storyboard_image 槽位） */
    workflowId?: string
  }>(),
  {
    allSelected: false,
    batchLoading: false,
    inflightCount: 0,
  },
)

const emit = defineEmits<{
  'update:workflowId': [string | undefined]
  'toggle-all': [boolean]
  generate: []
}>()

const someChecked = computed(() => props.selected > 0 && !props.allSelected)
/** n-checkbox 的 indeterminate 不是响应式绑定，单独同步 */
const indeterminate = ref(false)
watch(
  someChecked,
  (v) => {
    indeterminate.value = v
  },
  { immediate: true },
)
</script>

<template>
  <StageCard label="批量出图">
    <span class="wf-pick">
      <WorkflowSelector
        :model-value="workflowId"
        slot="storyboard_image"
        fixed-model-type="image_image_to_image"
        size="small"
        placeholder="选择分镜出图工作流"
        @update:model-value="(v: string | undefined) => emit('update:workflowId', v)"
      />
    </span>
    <n-checkbox
      :checked="allSelected"
      :indeterminate="indeterminate"
      :disabled="!total"
      @update:checked="(v: boolean) => emit('toggle-all', v)"
    >
      全选
    </n-checkbox>
    <span class="sel-count">已选 <b>{{ selected }}</b> / {{ total }}</span>

    <template #actions>
      <n-button
        size="small"
        type="primary"
        :disabled="!selected"
        :loading="batchLoading"
        @click="emit('generate')"
      >
        批量生成（{{ selected }}）
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

@media (max-width: 768px) {
  .wf-pick {
    flex: 1 1 100%;
    max-width: none;
  }
}
</style>
