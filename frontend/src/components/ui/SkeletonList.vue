<script setup lang="ts">
/** 骨架屏：首次加载用，固定占位避免布局抖动 */
withDefaults(
  defineProps<{
    rows?: number
    /** card = 卡片网格（资产/镜头），row = 列表行（任务/记录） */
    variant?: 'card' | 'row'
    columns?: number
    height?: string
  }>(),
  { rows: 6, variant: 'card', columns: 4, height: '132px' }
)
</script>

<template>
  <div
    class="skeleton-wrap"
    :class="variant"
    :style="variant === 'card' ? { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` } : undefined"
    aria-hidden="true"
  >
    <div
      v-for="i in rows"
      :key="i"
      class="skeleton-item"
      :style="variant === 'row' ? { height: '44px' } : { height }"
    >
      <span class="shimmer" />
    </div>
  </div>
</template>

<style scoped>
.skeleton-wrap {
  display: grid;
  gap: var(--sp-4);
}
.skeleton-wrap.row {
  grid-template-columns: 1fr;
  gap: var(--sp-2);
}
.skeleton-item {
  position: relative;
  overflow: hidden;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.shimmer {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    var(--overlay-hover) 45%,
    var(--overlay-active) 55%,
    transparent 100%
  );
  transform: translateX(-100%);
  animation: skeleton-slide 1.5s ease-in-out infinite;
}
@keyframes skeleton-slide {
  100% {
    transform: translateX(100%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .shimmer {
    animation: none;
    background: var(--overlay-hover);
  }
}
</style>
