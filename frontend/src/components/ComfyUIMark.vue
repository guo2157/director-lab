<script setup lang="ts">
/**
 * ComfyUI 品牌标记（2026-11-24 第二版）。
 *
 * v1 用「三个圆点 + 两条连线」→ 视觉复核判为**分享/分支图标**，不是节点图：
 * 分享图标的标准形态就是三点两线，形状撞车了。
 * v2 改成 ComfyUI 真正的语义——**两个输入节点汇聚到一个输出节点**，
 * 节点用圆角矩形（工作流节点的真实形态，不是圆点），连线走直角折线（工作流连线的真实走法）。
 * 这样形状与「分享」明确区分，同时保留「node graph」的可辨识性。
 *
 * 容器是品牌渐变圆角方块（与左侧 logo.svg 同一视觉语言：两端各一枚渐变标记），
 * 图形用 --text-on-brand 深色，在亮渐变上对比度足够（9.68:1）。
 */
withDefaults(defineProps<{ size?: number }>(), { size: 26 })
</script>

<template>
  <svg
    class="comfy-mark"
    :width="size"
    :height="size"
    viewBox="0 0 28 28"
    fill="none"
    aria-hidden="true"
    focusable="false"
  >
    <defs>
      <linearGradient id="comfyMarkGrad" x1="2" y1="1" x2="26" y2="27" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="var(--brand-primary)" />
        <stop offset="1" stop-color="var(--brand-mint)" />
      </linearGradient>
    </defs>
    <rect x="0" y="0" width="28" height="28" rx="7.5" fill="url(#comfyMarkGrad)" />
    <!-- 连线：两个输入节点的右缘 → 折向输出节点左缘。直角折线，是工作流连线的真实走法 -->
    <path
      d="M13.1 9.4h2.2v4.6M13.1 18.6h2.2v-4.6"
      stroke="var(--text-on-brand)"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      opacity="0.9"
    />
    <!-- 输入节点 ×2（左上 / 左下） -->
    <rect x="5.4" y="6.9" width="7.7" height="5" rx="1.7" fill="var(--text-on-brand)" />
    <rect x="5.4" y="16.1" width="7.7" height="5" rx="1.7" fill="var(--text-on-brand)" />
    <!-- 输出节点（右侧居中，略大一点表达汇聚） -->
    <rect x="15.3" y="11.1" width="7.9" height="5.8" rx="1.9" fill="var(--text-on-brand)" />
  </svg>
</template>

<style scoped>
.comfy-mark {
  display: block;
  flex: none;
}
</style>
