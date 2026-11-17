<script setup lang="ts">
/**
 * 批量工具栏：全选 / 已选计数 / 批量生成，固定在标签页上方。
 * 本批任务的进度与失败重试也收在这里——批量操作的反馈就贴着批量操作的按钮。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NCheckbox, NProgress } from 'naive-ui'
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
  }>(),
  {
    scopeLabel: '',
    batchLoading: false,
    inflightCount: 0,
    batch: null,
    retrying: false,
    failedCount: 0,
  },
)

const emit = defineEmits<{
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
  <section class="batch-bar">
    <n-checkbox
      :checked="allChecked"
      :indeterminate="indeterminate"
      :disabled="!total"
      @update:checked="(v: boolean) => emit('toggle-all', v)"
    >
      全选
    </n-checkbox>
    <span class="sel-count">
      已选 <b>{{ selected }}</b> / {{ total }}
    </span>
    <n-button
      size="small"
      type="primary"
      :loading="batchLoading"
      :disabled="!selected"
      @click="emit('batch-generate')"
    >
      批量生成{{ scopeLabel }}（{{ selected }}）
    </n-button>
    <span v-if="inflightCount" class="inflight">排队/生成中 {{ inflightCount }} 个</span>

    <span class="spacer" />

    <template v-if="batch">
      <span class="batch-nums">
        本批 {{ batch.done }}/{{ batch.total }} · 生成中 {{ batch.running }}<template v-if="batch.failed"> · 失败 {{ batch.failed }}</template>
      </span>
      <n-progress
        class="batch-bar-line"
        type="line"
        :percentage="batch.percent"
        :height="4"
        :border-radius="2"
        :show-indicator="false"
        :status="batch.failed && !batch.running ? 'error' : 'default'"
        rail-color="var(--border-subtle)"
      />
      <n-button
        v-if="batch.failed"
        size="tiny"
        quaternary
        type="warning"
        :loading="retrying"
        @click="emit('retry-failed')"
      >
        重试失败项
      </n-button>
      <n-button size="tiny" quaternary @click="emit('clear-batch')">收起</n-button>
    </template>

    <span v-else-if="failedCount" class="failed">
      {{ failedCount }} 个生成失败
      <n-button size="tiny" quaternary type="warning" :loading="retrying" @click="emit('retry-failed')">
        重试
      </n-button>
    </span>
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
  margin-bottom: var(--sp-3);
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
.spacer {
  flex: 1;
  min-width: var(--sp-2);
}
.batch-nums {
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
}
.batch-bar-line {
  width: 96px;
  flex: none;
}
.failed {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--state-danger);
}
@media (max-width: 900px) {
  .batch-bar {
    flex-wrap: wrap;
  }
  .batch-bar-line {
    display: none;
  }
}
</style>
