<script setup lang="ts">
/**
 * 工作流测试与版本面板（P5/P3 UI）：选中工作流 → 依赖检查（真实 /object_info）
 * → 测试提交（真实 202 + prompt_id）→ 版本历史（发布/回滚）。
 * 不伪造结果：缺依赖、缺必需媒体都会原样展示原因。
 */
import { computed, ref } from 'vue'
import {
  NAlert, NButton, NCard, NDescriptions, NDescriptionsItem, NSpin, NTag, useMessage,
} from 'naive-ui'
import WorkflowSelector from '@/components/workflow/WorkflowSelector.vue'
import {
  checkWorkflowRuntime,
  listWorkflowVersions,
  publishWorkflowVersion,
  rollbackWorkflowVersion,
  testWorkflow,
} from '@/api/workflows'
import type { RuntimeCheckResult, WorkflowVersionItem } from '@/types/workflow'

const message = useMessage()
const workflowId = ref<string | undefined>(undefined)
const checking = ref(false)
const testing = ref(false)
const loadingVersions = ref(false)
const runtime = ref<RuntimeCheckResult | null>(null)
const testResult = ref<{ prompt_id: string; pruned_slots: string[] } | null>(null)
const versions = ref<WorkflowVersionItem[]>([])

const statusType = computed(() => {
  const s = runtime.value?.runtime_status
  if (s === 'ready') return 'success' as const
  if (s === 'unreachable') return 'warning' as const
  if (s === 'missing_deps') return 'error' as const
  return 'default' as const
})

const statusText = computed(() => {
  const s = runtime.value?.runtime_status
  return {
    ready: '依赖齐全，可提交',
    missing_deps: '缺节点或缺模型，无法提交',
    unreachable: '实例不可达（不代表工作流有问题）',
    unchecked: '未检查',
  }[s ?? 'unchecked'] ?? '未检查'
})

async function onSelected() {
  runtime.value = null
  testResult.value = null
  versions.value = []
  if (!workflowId.value) return
  loadingVersions.value = true
  try {
    versions.value = await listWorkflowVersions(workflowId.value)
  } catch (e: unknown) {
    message.error(`读取版本失败：${e instanceof Error ? e.message : e}`)
  } finally {
    loadingVersions.value = false
  }
}

async function onCheck() {
  if (!workflowId.value) return
  checking.value = true
  try {
    runtime.value = await checkWorkflowRuntime(workflowId.value)
    message[runtime.value.runtime_status === 'ready' ? 'success' : 'warning'](statusText.value)
  } catch (e: unknown) {
    message.error(`检查失败：${e instanceof Error ? e.message : e}`)
  } finally {
    checking.value = false
  }
}

async function onTest() {
  if (!workflowId.value) return
  testing.value = true
  testResult.value = null
  try {
    const res = await testWorkflow(workflowId.value)
    testResult.value = { prompt_id: res.prompt_id, pruned_slots: res.pruned_slots ?? [] }
    message.success('测试任务已提交到 ComfyUI')
  } catch (e: unknown) {
    message.error(`测试提交被拒绝：${e instanceof Error ? e.message : e}`)
  } finally {
    testing.value = false
  }
}

async function onPublish(versionId: string) {
  if (!workflowId.value) return
  try {
    await publishWorkflowVersion(workflowId.value, versionId)
    message.success('已发布该版本')
    await onSelected()
  } catch (e: unknown) {
    message.error(`发布失败：${e instanceof Error ? e.message : e}`)
  }
}

async function onRollback(versionId: string) {
  if (!workflowId.value) return
  try {
    await rollbackWorkflowVersion(workflowId.value, versionId)
    message.success('已回滚到该版本')
    await onSelected()
  } catch (e: unknown) {
    message.error(`回滚失败：${e instanceof Error ? e.message : e}`)
  }
}
</script>

<template>
  <NCard class="settings-card" :bordered="false" title="工作流测试与版本">
    <div class="wt-row">
      <WorkflowSelector
        v-model="workflowId"
        placeholder="选择要检查的工作流"
        @selected="onSelected"
      />
      <NButton :loading="checking" :disabled="!workflowId" @click="onCheck">依赖检查</NButton>
      <NButton type="primary" :loading="testing" :disabled="!workflowId" @click="onTest">
        测试提交
      </NButton>
    </div>

    <NSpin v-if="checking" :size="14" />

    <NAlert v-if="runtime" :type="statusType" :bordered="false" class="wt-alert">
      <div class="wt-line">
        <NTag size="small" :bordered="false" :type="statusType">{{ statusText }}</NTag>
        <span class="wt-meta">实例 {{ runtime.instance || '—' }}</span>
        <span class="wt-meta">
          节点 {{ runtime.required_nodes ?? '—' }} 需求 / 实例 {{ runtime.checked_nodes ?? '—' }}
        </span>
      </div>
      <div v-if="runtime.missing_node_types.length" class="wt-miss">
        缺节点：{{ runtime.missing_node_types.join('、') }}
      </div>
      <div v-if="runtime.missing_models.length" class="wt-miss">
        缺模型：{{ runtime.missing_models.join('、') }}
      </div>
      <div v-if="runtime.error" class="wt-miss">{{ runtime.error }}</div>
    </NAlert>

    <NAlert v-if="testResult" type="info" :bordered="false" class="wt-alert">
      <div>测试任务已提交，prompt_id：<code>{{ testResult.prompt_id }}</code></div>
      <div v-if="testResult.pruned_slots.length" class="wt-meta">
        已剔除未注入槽位：{{ testResult.pruned_slots.join('、') }}
      </div>
      <div class="wt-meta">用 GET /api/workflows/test/{{ testResult.prompt_id }} 查询结果</div>
    </NAlert>

    <NDescriptions v-if="versions.length" :column="1" size="small" bordered class="wt-versions">
      <NDescriptionsItem v-for="v in versions" :key="v.id" :label="`v${v.version}`">
        <div class="wt-ver">
          <NTag v-if="v.is_current" size="tiny" type="success" :bordered="false">当前</NTag>
          <NTag size="tiny" :bordered="false">{{ v.validation_status }}</NTag>
          <NTag size="tiny" :bordered="false">{{ v.runtime_status }}</NTag>
          <span class="wt-meta">{{ v.changelog || '—' }}</span>
          <span class="wt-meta">参数 {{ v.schema_summary.params }} · 输出 {{ v.schema_summary.outputs.join(',') || '—' }}</span>
          <NButton v-if="!v.is_current" size="tiny" quaternary @click="onPublish(v.id)">发布</NButton>
          <NButton v-if="!v.is_current" size="tiny" quaternary @click="onRollback(v.id)">回滚</NButton>
        </div>
      </NDescriptionsItem>
    </NDescriptions>
  </NCard>
</template>

<style scoped>
.wt-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-2);
}

.wt-alert {
  margin-top: var(--sp-2);
}

.wt-line {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.wt-meta {
  font-size: 12px;
  color: var(--text-secondary);
}

.wt-miss {
  margin-top: 4px;
  font-size: 12px;
}

.wt-versions {
  margin-top: var(--sp-3);
}

.wt-ver {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
</style>
