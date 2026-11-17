<script setup lang="ts">
/**
 * 资产库状态条：一行回答三件事——做到哪了（进度）、能不能往下走（审核状态）、下一步点什么。
 * 主按钮始终只有一个（去分镜图）；审核通过/驳回返工降级为同一条内的次要操作。
 */
import { NButton, NIcon, NProgress } from 'naive-ui'
import { ArrowForwardOutline } from '@vicons/ionicons5'
import PageHeader from '@/components/ui/PageHeader.vue'
import StatusTag from '@/components/ui/StatusTag.vue'
import type { ProgressGroupView } from './types'

const props = withDefaults(
  defineProps<{
    title: string
    eyebrow?: string
    /** 生成状态一句话（正在生成什么 / 已全部生成完成） */
    headline?: string
    done: number
    total: number
    allDone: boolean
    reviewStatus: string
    reviewText: string
    /** 角色 / 场景 / 道具分项进度 */
    groups?: ProgressGroupView[]
    inflightCount?: number
    /** 审核请求进行中 */
    reviewing?: boolean
    /** 进度刷新中 */
    loading?: boolean
  }>(),
  {
    eyebrow: '',
    headline: '',
    groups: () => [],
    inflightCount: 0,
    reviewing: false,
    loading: false,
  },
)

const emit = defineEmits<{
  approve: []
  reject: []
  next: []
  refresh: []
}>()

const percent = () => (props.total ? Math.round((props.done / props.total) * 100) : 0)
const reviewKind = () => {
  if (props.reviewStatus === 'approved') return 'succeeded' as const
  if (props.reviewStatus === 'rejected') return 'failed' as const
  return 'pending' as const
}
const reviewLabel = () => {
  if (props.reviewStatus === 'approved') return '审核通过'
  if (props.reviewStatus === 'rejected') return '已驳回'
  return '待审核'
}
</script>

<template>
  <section class="status-bar">
    <PageHeader :title="title" :eyebrow="eyebrow">
      <template #subtitle>{{ headline }}</template>

      <template #status>
        <span class="prog-nums" :class="{ 'is-done': allDone }">
          <b>{{ done }}</b><span class="prog-sep">/</span>{{ total }}
          <span class="prog-pct">{{ percent() }}%</span>
        </span>
        <n-progress
          type="line"
          class="prog-bar"
          :percentage="percent()"
          :height="4"
          :border-radius="2"
          :show-indicator="false"
          :color="allDone ? 'var(--state-success)' : 'var(--brand-primary)'"
          rail-color="var(--border-subtle)"
        />
        <StatusTag :status="reviewKind()" :label="reviewLabel()" />
      </template>

      <template #actions>
        <n-button
          size="small"
          quaternary
          :loading="loading"
          @click="emit('refresh')"
        >
          刷新进度
        </n-button>
        <n-button
          size="small"
          quaternary
          :disabled="reviewStatus !== 'approved'"
          :loading="reviewing"
          @click="emit('reject')"
        >
          驳回返工
        </n-button>
        <n-button
          size="small"
          secondary
          type="primary"
          :disabled="!allDone"
          :loading="reviewing"
          @click="emit('approve')"
        >
          审核通过
        </n-button>
        <n-button
          size="small"
          type="primary"
          :disabled="reviewStatus !== 'approved'"
          @click="emit('next')"
        >
          去分镜图 <n-icon :component="ArrowForwardOutline" :size="14" />
        </n-button>
      </template>
    </PageHeader>

    <div class="status-meta">
      <span
        v-for="g in groups"
        :key="g.label"
        class="meta-chip"
        :class="{ 'is-done': g.total > 0 && g.done === g.total }"
      >{{ g.label }} {{ g.done }}/{{ g.total }}</span>
      <span v-if="inflightCount" class="meta-inflight">生成中 {{ inflightCount }} 个</span>
      <span class="meta-text">{{ reviewText }}</span>
    </div>
  </section>
</template>

<style scoped>
.status-bar {
  margin-bottom: var(--sp-4);
}
.prog-nums {
  display: inline-flex;
  align-items: baseline;
  gap: 2px;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.prog-nums b {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.prog-nums.is-done b {
  color: var(--state-success);
}
.prog-sep {
  color: var(--text-tertiary);
}
.prog-pct {
  margin-left: var(--sp-2);
  font-size: 12px;
  color: var(--text-tertiary);
}
.prog-bar {
  width: 84px;
  flex: none;
}
.status-bar :deep(.page-header) {
  margin-bottom: var(--sp-3);
}
.status-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
}
.meta-chip {
  padding: 0 var(--sp-2);
  font-size: 12px;
  line-height: 19px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
.meta-chip.is-done {
  color: var(--state-success);
  background: var(--success-soft);
  border-color: var(--success-line);
}
.meta-inflight {
  font-size: 12px;
  color: var(--state-generating);
}
.meta-text {
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-secondary);
}
</style>
