<script setup lang="ts">
/**
 * 资产库顶部两卡之一：执行进度（只读）+ 推进闸口。
 *
 * 卡内只留三样：总分 X/Y + 进度条 + 在途/失败状态。
 * 「公共资产图已全部生成完成（最近完成于 04:49:04）」「角色定妆图 2/2 / 场景基准图 5/5 /
 * 道具锚定图 4/4」已删除——总分与进度条已经回答同一件事。
 *
 * 人工审核已取消：审核状态标签、审核通过 / 驳回返工按钮、审核说明句全部删除。
 * 资产图的把关动作改为「去分镜图」时的一次确认弹窗——弹窗里明确写出「确认图片都已
 * 审核过没问题」，确认后直接进入分镜图。比在页面顶部维护一套 pending/approved/rejected
 * 状态机更贴近实际用法：资产是一次性看完再推进，不需要留驳回痕迹。
 *
 * 必要条件：资产图全部生成完成（allDone）。未完成时按钮禁用并说明还差几项。
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
    inflightCount?: number
    /** 进度刷新中 */
    loading?: boolean
  }>(),
  {
    inflightCount: 0,
    loading: false,
  },
)

const emit = defineEmits<{
  next: []
  refresh: []
}>()

const percent = () => (props.total ? Math.round((props.done / props.total) * 100) : 0)
/**
 * 跳转按钮**不禁用**：任务书 §3.4 要求「点击后检查生成情况，未完成时提示缺哪些图」。
 * 禁用按钮会让用户点不到这条提示，只能看到一个灰按钮却不知道为什么。
 * 真正的拦截在后端 stage-readiness（就绪未通过则不弹窗、不跳转）。
 */
const nextTitle = () => {
  if (props.total === 0) return '当前项目还没有资产，请先在解析页完成解析并生成资产'
  if (!props.allDone) return `还有 ${props.total - props.done} 项资产未生成完成，点击查看缺失明细`
  return '确认资产图无误后进入分镜图生成'
}
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
    <StatusTag
      v-if="inflightCount"
      status="generating"
      :label="`生成中 ${inflightCount}`"
    />

    <template #actions>
      <n-button size="small" quaternary :loading="loading" @click="emit('refresh')">
        刷新进度
      </n-button>
      <n-button
        size="small"
        type="primary"
        :title="nextTitle()"
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
</style>
