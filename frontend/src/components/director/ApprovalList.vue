<script setup lang="ts">
/**
 * 待审批生成列表：批准（只放行）与执行（才建任务）分离。
 * 面板只展示关键参数（目标类型 + 镜头），避免把整段入参铺满侧栏。
 */
import { NButton, NTag } from 'naive-ui'
import type { AgentApproval } from '@/api/agent'
import SkeletonList from '@/components/ui/SkeletonList.vue'

const props = defineProps<{
  approvals: AgentApproval[]
  loading: boolean
  approvingId: string | null
}>()

const emit = defineEmits<{
  decide: [approval: AgentApproval, approve: boolean]
  run: [approval: AgentApproval]
}>()

function summary(a: AgentApproval): string {
  const args = (a.args ?? {}) as Record<string, unknown>
  const target = String(args.target ?? '')
  const shot = String(args.shot_id ?? '')
  return [target || '生成', shot].filter(Boolean).join(' · ')
}

function isBusy(a: AgentApproval): boolean {
  return props.approvingId === a.confirmation_id
}
</script>

<template>
  <SkeletonList v-if="loading && !approvals.length" variant="row" :rows="2" />
  <p v-else-if="!approvals.length" class="approval-empty">暂无待审批申请</p>
  <div v-else class="approval-list">
    <div v-for="a in approvals" :key="a.confirmation_id" class="approval-item">
      <span class="approval-title" :title="a.tool_name">{{ summary(a) }}</span>
      <div class="approval-actions">
        <template v-if="a.status === 'pending'">
          <NButton size="tiny" type="primary" :loading="isBusy(a)" @click="emit('decide', a, true)">
            批准
          </NButton>
          <NButton size="tiny" :loading="isBusy(a)" @click="emit('decide', a, false)">拒绝</NButton>
        </template>
        <template v-else-if="a.status === 'approved'">
          <NTag size="tiny" type="success">已批准</NTag>
          <NButton size="tiny" type="primary" :loading="isBusy(a)" @click="emit('run', a)">
            执行
          </NButton>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.approval-empty {
  margin: 0;
  padding: var(--sp-2) 0;
  font-size: 12px;
  color: var(--text-tertiary);
}

.approval-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.approval-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}

.approval-title {
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-actions {
  display: flex;
  gap: var(--sp-1);
  flex-shrink: 0;
}
</style>
