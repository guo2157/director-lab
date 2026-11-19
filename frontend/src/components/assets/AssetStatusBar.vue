<script setup lang="ts">
/**
 * 资产库顶部两卡之一：执行进度（只读）。
 *
 * 改造前这里是四条独立横幅：PageHeader 状态行 / 生成状态句 / 分项 chips / 审核说明句，
 * 加上 AssetBatchToolbar 自成一条，视觉上四层横条叠加，层级相同、信息互相重复。
 * 现在：进度卡只回答「做到哪了」，操作卡只回答「接下来批量做什么」。
 *
 * 卡内只留三样：总分 X/Y + 进度条 + 审核状态标签。
 * 「公共资产图已全部生成完成（最近完成于 04:49:04）」「角色定妆图 2/2 / 场景基准图 5/5 /
 * 道具锚定图 4/4」已删除——总分与进度条已经回答同一件事，分项拆解属于资产卡片自身信息，
 * 放在进度条旁边只是把「已完成」这个结论重复三遍。
 *
 * 审核动作（通过 / 驳回返工）留在进度卡右端：它是「放行下一步」的闸门，
 * 与「去分镜图」同属推进流程，不属于批量生成。
 */
import { NButton, NIcon, NProgress } from 'naive-ui'
import { ArrowForwardOutline } from '@vicons/ionicons5'
import StageCard from '@/components/ui/StageCard.vue'
import StatusTag from '@/components/ui/StatusTag.vue'

const props = withDefaults(
  defineProps<{
    done: number
    total: number
    allDone: boolean
    reviewStatus: string
    reviewText: string
    inflightCount?: number
    /** 审核请求进行中 */
    reviewing?: boolean
    /** 进度刷新中 */
    loading?: boolean
  }>(),
  {
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
/** 未通过时才需要审核动作与说明；通过后这两个按钮/这句话都是已完成的事 */
const needsReview = () => props.reviewStatus !== 'approved'
</script>

<template>
  <StageCard label="执行进度">
    <span class="nums" :class="{ 'is-done': allDone }">
      <b>{{ done }}</b><span class="sep">/</span>{{ total }}
      <span class="pct">{{ percent() }}%</span>
    </span>
    <n-progress
      class="bar"
      type="line"
      :percentage="percent()"
      :height="4"
      :border-radius="2"
      :show-indicator="false"
      :color="allDone ? 'var(--state-success)' : 'var(--brand-primary)'"
      rail-color="var(--border-subtle)"
    />
    <StatusTag :status="reviewKind()" :label="reviewLabel()" />
    <span v-if="inflightCount" class="inflight">生成中 {{ inflightCount }}</span>

    <template #meta>
      <!-- 审核说明只在未通过时出现：通过后审核状态已由标签和主按钮表达。
           分项进度（角色 2/2 场景 5/5 道具 4/4）与「已全部生成完成」状态句已删除：
           总分 X/Y 与进度条已经回答「做到哪了」，分项拆解属于资产卡片自己的信息。 -->
      <span v-if="needsReview()" class="review-text">{{ reviewText }}</span>
    </template>

    <template #actions>
      <n-button size="small" quaternary :loading="loading" @click="emit('refresh')">
        刷新进度
      </n-button>
      <template v-if="needsReview()">
        <n-button
          size="small"
          quaternary
          :disabled="reviewStatus !== 'approved'"
          :loading="reviewing"
          :title="reviewStatus !== 'approved' ? '仅当资产审核通过后，才能驳回返工' : undefined"
          @click="emit('reject')"
        >
          驳回返工
        </n-button>
        <n-button
          size="small"
          type="primary"
          secondary
          :disabled="!allDone"
          :loading="reviewing"
          :title="!allDone ? (total === 0 ? '当前项目还没有资产，请先在解析页完成解析并生成资产' : `还有 ${total - done} 项资产未生成完成，全部完成后才能审核`) : undefined"
          @click="emit('approve')"
        >
          审核通过
        </n-button>
      </template>
      <n-button
        size="small"
        type="primary"
        :disabled="reviewStatus !== 'approved'"
        :title="reviewStatus !== 'approved' ? '资产审核通过后才能进入分镜图生成' : undefined"
        @click="emit('next')"
      >
        去分镜图 <n-icon :component="ArrowForwardOutline" :size="14" />
      </n-button>
    </template>
  </StageCard>
</template>

<style scoped>
.nums {
  display: inline-flex;
  align-items: baseline;
  gap: 2px;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-secondary);
  white-space: nowrap;
}
.nums b {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.nums.is-done b {
  color: var(--state-success);
}
.sep {
  color: var(--text-tertiary);
}
.pct {
  margin-left: var(--sp-2);
  font-size: 12px;
  color: var(--text-tertiary);
}
.bar {
  width: 84px;
  flex: none;
}
.inflight {
  font-size: 12px;
  color: var(--state-generating);
}
.review-text {
  color: var(--text-secondary);
}
</style>
