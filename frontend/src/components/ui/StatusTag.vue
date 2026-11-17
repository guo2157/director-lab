<script setup lang="ts">
/**
 * 统一状态标签：颜色 + 文字双通道，不靠颜色单独表意。
 * 语义色全部来自 global.css 的 .status-tag.is-* 令牌。
 */
type StatusKind =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'generating'
  | 'locked'
  | 'stale'

withDefaults(
  defineProps<{
    status: StatusKind
    label: string
    /** 生成中是否显示脉冲动效 */
    pulse?: boolean
  }>(),
  { pulse: false }
)
</script>

<template>
  <span
    class="status-tag"
    :class="[`is-${status}`, { 'is-generating': status === 'generating' && pulse }]"
    role="status"
  >
    <span class="dot" aria-hidden="true" />
    {{ label }}
  </span>
</template>

<style scoped>
.status-tag {
  gap: var(--sp-2);
}
.dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}
</style>
