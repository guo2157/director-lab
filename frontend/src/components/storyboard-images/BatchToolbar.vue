<script setup lang="ts">
/**
 * 分镜图批量工具栏：全选 / 状态筛选 / 批量生成 / 批量审核通过。
 * 吸附在页面顶部——滚动 26 个镜头时，批量操作始终在手边。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox } from 'naive-ui'
import { SHOT_FILTERS, type ShotFilter } from './types'

const props = withDefaults(
  defineProps<{
    total: number
    selected: number
    allSelected?: boolean
    filter: ShotFilter
    counts: Record<ShotFilter, number>
    /** 公共资产审核通过才可生成 */
    canGenerate?: boolean
    batchLoading?: boolean
    confirmLoading?: boolean
    inflightCount?: number
  }>(),
  {
    allSelected: false,
    canGenerate: true,
    batchLoading: false,
    confirmLoading: false,
    inflightCount: 0,
  },
)

const emit = defineEmits<{
  'update:filter': [ShotFilter]
  'toggle-all': [boolean]
  generate: []
  confirm: []
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
  <section class="batch-bar">
    <n-checkbox
      :checked="allSelected"
      :indeterminate="indeterminate"
      :disabled="!total"
      @update:checked="(v: boolean) => emit('toggle-all', v)"
    >
      全选
    </n-checkbox>
    <span class="sel-count">已选 <b>{{ selected }}</b> / {{ total }}</span>
    <span v-if="inflightCount" class="inflight">排队 / 生成中 {{ inflightCount }}</span>

    <span class="divider" />

    <div class="filters" role="tablist" aria-label="按状态筛选镜头">
      <button
        v-for="f in SHOT_FILTERS"
        :key="f.value"
        type="button"
        role="tab"
        class="f-btn"
        :class="{ active: filter === f.value }"
        :aria-selected="filter === f.value"
        @click="emit('update:filter', f.value)"
      >
        {{ f.label }}
        <span class="f-count">{{ counts[f.value] ?? 0 }}</span>
      </button>
    </div>

    <span class="spacer" />

    <n-button
      size="small"
      type="primary"
      secondary
      :disabled="!selected"
      :loading="confirmLoading"
      @click="emit('confirm')"
    >
      批量审核通过（{{ selected }}）
    </n-button>
    <n-button
      size="small"
      type="primary"
      :disabled="!canGenerate || !selected"
      :loading="batchLoading"
      @click="emit('generate')"
    >
      批量生成（{{ selected }}）
    </n-button>
  </section>
</template>

<style scoped>
.batch-bar {
  position: sticky;
  top: 0;
  z-index: 4;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.sel-count {
  font-size: 12px;
  color: var(--text-secondary);
}
.sel-count b {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-primary);
}
.inflight {
  font-size: 12px;
  color: var(--state-generating);
}
.divider {
  width: 1px;
  height: 16px;
  background: var(--border-subtle);
}
.filters {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
}
.f-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px var(--sp-2);
  font-family: inherit;
  font-size: 12px;
  line-height: 20px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
}
.f-btn:hover {
  color: var(--text-primary);
  background: var(--overlay-hover);
}
.f-btn.active {
  color: var(--brand-hover);
  background: var(--brand-soft);
  border-color: var(--brand-line);
}
.f-count {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
}
.f-btn.active .f-count {
  color: var(--brand-hover);
}
.spacer {
  flex: 1;
  min-width: var(--sp-2);
}
@media (max-width: 768px) {
  .spacer {
    display: none;
  }
  .filters {
    order: 3;
    width: 100%;
    overflow-x: auto;
  }
}
</style>
