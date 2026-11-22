<script setup lang="ts">
/**
 * 依赖影响预演面板（Phase 3，只读）。
 *
 * 后端 `impact-preview` 为纯只读计算：不标记失效、不删除产物、不自动重生成。
 * 因此本面板只提供「查询 + 展示」，把重做/重绑定动作留给用户在其他入口显式触发。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NTag } from 'naive-ui'
import {
  getDependencyImpactPreview,
  type DependencyChangeStatus,
  type DependencyImpactPreview,
} from '@/api/dependencies'

const props = defineProps<{
  projectId: string
  nodeType: string
  nodeId: string | null
  /** 节点切换时是否自动查询；默认手动触发，避免打开详情就打一次依赖图 */
  autoLoad?: boolean
}>()

const loading = ref(false)
const error = ref<string | null>(null)
const impact = ref<DependencyImpactPreview | null>(null)

const CHANGE_STATUS_LABEL: Record<DependencyChangeStatus, string> = {
  redo_required: '需重生成',
  rebind_required: '需重绑定',
  not_generated: '未生成',
  stale_reference: '引用过期',
  unknown_version: '版本未知',
  up_to_date: '已最新',
}
const NODE_TYPE_LABEL: Record<string, string> = {
  asset: '资产',
  shot: '镜头',
  job: '任务',
  composition: '合成',
  export: '导出',
}

const canLoad = computed(() => Boolean(props.projectId && props.nodeId))

function nodeLabel(node: { type: string; id: string }): string {
  return `${NODE_TYPE_LABEL[node.type] ?? node.type} ${node.id}`
}

async function load() {
  if (!canLoad.value || !props.nodeId) return
  loading.value = true
  error.value = null
  try {
    impact.value = await getDependencyImpactPreview(props.projectId, props.nodeType, props.nodeId)
  } catch (e: any) {
    impact.value = null
    error.value = e?.message ?? '加载依赖影响失败'
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.nodeType, props.nodeId] as const,
  () => {
    impact.value = null
    error.value = null
    if (props.autoLoad) void load()
  },
)

defineExpose({ load })
</script>

<template>
  <div class="impact">
    <div class="impact-head">
      <n-button size="tiny" secondary :loading="loading" :disabled="!canLoad" @click="load">
        检查变更影响
      </n-button>
      <span v-if="impact" class="impact-sum">
        下游受影响 {{ impact.summary.affected_count }} 项 ·
        需重生成 {{ impact.summary.redo_required }} ·
        需重绑定 {{ impact.summary.rebind_required }}
      </span>
    </div>

    <p v-if="error" class="impact-note is-error">{{ error }}</p>
    <template v-else-if="impact">
      <div v-if="impact.affected.length" class="impact-list">
        <div v-for="node in impact.affected" :key="node.key" class="impact-item">
          <span class="impact-depth">L{{ node.depth }}</span>
          <span class="impact-name">{{ nodeLabel(node) }}</span>
          <n-tag
            size="tiny"
            :bordered="false"
            :type="node.change_status === 'up_to_date' ? 'success' : 'warning'"
          >
            {{ CHANGE_STATUS_LABEL[node.change_status] }}
          </n-tag>
          <span v-if="node.stale_inputs.length" class="impact-reason">
            {{ node.stale_inputs.map((s) => s.reason).join(' / ') }}
          </span>
        </div>
      </div>
      <p v-else class="impact-note">当前没有下游对象受此节点变更影响。</p>
      <p class="impact-policy">{{ impact.policy.note }}</p>
    </template>
  </div>
</template>

<style scoped>
.impact {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.impact-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.impact-sum {
  font-size: 12px;
  color: var(--text-tertiary);
}
.impact-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.impact-note.is-error {
  color: var(--state-error);
}
.impact-policy {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
.impact-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.impact-item {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  font-size: 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.impact-depth {
  flex: none;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
.impact-name {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.impact-reason {
  flex: none;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
</style>
