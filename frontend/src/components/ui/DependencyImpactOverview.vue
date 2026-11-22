<script setup lang="ts">
/**
 * 项目级依赖影响总览（Phase 3，只读）。
 *
 * 与单节点面板共用后端分类逻辑：把所有「已过期依赖的上游」作为变化源，
 * 一次性算出全项目待重做/待重绑对象。仅在存在待处理项时展示，避免常驻噪音。
 */
import { computed, ref } from 'vue'
import { NButton, NTag } from 'naive-ui'
import {
  getProjectImpactPreview,
  IMPACT_NODE_TYPE_LABEL,
  IMPACT_STATUS_LABEL,
  type DependencyProjectImpactPreview,
} from '@/api/dependencies'

const props = defineProps<{ projectId: string }>()

const emit = defineEmits<{
  /** 每次检查完成后抛出原始数据，供页面派生镜头级常驻标记 */
  (e: 'loaded', data: DependencyProjectImpactPreview): void
}>()

const loading = ref(false)
const error = ref<string | null>(null)
const data = ref<DependencyProjectImpactPreview | null>(null)
const expanded = ref(false)

/** 只看需要用户处理的状态，未生成/已最新不进入待办清单 */
const pending = computed(
  () =>
    data.value?.affected.filter(
      (node) =>
        node.change_status === 'redo_required' ||
        node.change_status === 'rebind_required' ||
        node.change_status === 'stale_reference',
    ) ?? [],
)

const headline = computed(() => {
  if (!data.value) return ''
  const { redo_required, rebind_required } = data.value.summary
  const parts: string[] = []
  if (redo_required) parts.push(`${redo_required} 项需重生成`)
  if (rebind_required) parts.push(`${rebind_required} 项需重绑定`)
  return parts.join('，')
})

function nodeLabel(node: { type: string; id: string }): string {
  return `${IMPACT_NODE_TYPE_LABEL[node.type] ?? node.type} ${node.id}`
}

async function load() {
  if (!props.projectId) return
  loading.value = true
  error.value = null
  try {
    data.value = await getProjectImpactPreview(props.projectId)
    emit('loaded', data.value)
    if (!pending.value.length) expanded.value = false
  } catch (e: any) {
    data.value = null
    error.value = e?.message ?? '加载依赖影响总览失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="overview">
    <div class="overview-head">
      <n-button size="tiny" secondary :loading="loading" @click="load">检查依赖影响</n-button>
      <span v-if="headline" class="overview-text">{{ headline }}</span>
      <span v-else-if="data" class="overview-text is-ok">上游变化未影响任何下游产物</span>
      <n-button
        v-if="pending.length"
        size="tiny"
        quaternary
        @click="expanded = !expanded"
      >{{ expanded ? '收起' : `查看 ${pending.length} 项` }}</n-button>
    </div>

    <p v-if="error" class="overview-note is-error">{{ error }}</p>

    <div v-else-if="expanded && pending.length" class="overview-list">
      <div v-for="node in pending" :key="node.key" class="overview-item">
        <span class="overview-depth">L{{ node.depth }}</span>
        <span class="overview-name">{{ nodeLabel(node) }}</span>
        <n-tag
          size="tiny"
          :bordered="false"
          :type="node.change_status === 'redo_required' ? 'warning' : 'info'"
        >
          {{ IMPACT_STATUS_LABEL[node.change_status] }}
        </n-tag>
        <span class="overview-reason">
          {{ node.stale_inputs.map((s) => s.source).join(', ') }}
        </span>
      </div>
      <p class="overview-note">{{ data?.policy.note }}</p>
    </div>
  </div>
</template>

<style scoped>
.overview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.overview-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.overview-text {
  font-size: 12px;
  color: var(--state-warning);
}
.overview-text.is-ok {
  color: var(--text-tertiary);
}
.overview-note {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
.overview-note.is-error {
  color: var(--danger-text);
}
.overview-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.overview-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  font-size: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.overview-depth {
  flex: none;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
.overview-name {
  flex: none;
  font-family: var(--font-mono);
}
.overview-reason {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
