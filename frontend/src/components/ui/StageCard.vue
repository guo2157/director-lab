<script setup lang="ts">
/**
 * 阶段卡片：把项目内步骤页顶部从「多个独立横幅堆叠」收敛为「标签 + 内容」的账本式结构。
 *
 * 设计意图：顶部只回答两个问题——
 *   执行进度（做到了哪）：只读，含状态、数字、进度条、刷新与去下一步；
 *   批量操作（接下来做什么）：可写，含工作流、全选、批量生成。
 * 两者语义不同（观察 / 操作），因此拆成两张卡，而不是塞进一条工具栏。
 *
 * 标签用左侧固定列而非卡片标题行：卡片标题行会在每张卡上方多出一条 12px 的字，
 * 两张卡就多出两行「eyebrow」，正是本次要消除的视觉噪音。左侧列同时给两张卡
 * 建立共同的对齐轴，让它们读起来是一套系统而不是两个孤立的框。
 *
 * 布局用 flex 而非 grid：曾用 `grid-template-columns: 60px 1fr auto`，
 * 但 meta 用 `grid-column: 2 / -1` 跨列时会与第三列争抢宽度，
 * 导致操作列塌缩到 60px（比它自己的按钮还窄），按钮被迫竖排、卡片被撑高。
 */
withDefaults(
  defineProps<{
    /** 左侧固定列标签，2–5 字 */
    label: string
  }>(),
  {},
)
</script>

<template>
  <section class="stage-card">
    <div class="stage-row">
      <span class="stage-label">{{ label }}</span>
      <div class="stage-body">
        <slot />
      </div>
      <div v-if="$slots.actions" class="stage-actions">
        <slot name="actions" />
      </div>
    </div>
    <div v-if="$slots.meta" class="stage-meta">
      <slot name="meta" />
    </div>
  </section>
</template>

<style scoped>
.stage-card {
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-3);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-sheen);
}

.stage-row {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  min-width: 0;
}

/* 左侧标签列：固定宽度，两张卡靠它对齐成同一套系统 */
.stage-label {
  flex: 0 0 60px;
  font-size: 12px;
  line-height: 24px;
  letter-spacing: 0.06em;
  color: var(--text-tertiary);
  white-space: nowrap;
}

.stage-body {
  display: flex;
  align-items: center;
  gap: var(--sp-2) var(--sp-3);
  flex-wrap: wrap;
  flex: 1 1 auto;
  min-width: 0;
}

/* 操作区：不参与收缩，按钮永远横排；空间不够时整体折到下一行 */
.stage-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex: 0 0 auto;
  margin-left: auto;
}

/* 第二行（分项明细 / 在途任务 / 筛选）：与内容列左边缘对齐 */
.stage-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2) var(--sp-3);
  flex-wrap: wrap;
  margin-top: var(--sp-2);
  padding-left: calc(60px + var(--sp-4));
  font-size: 12px;
  line-height: 19px;
  color: var(--text-secondary);
  min-width: 0;
}

@media (max-width: 768px) {
  /* 窄屏压缩：改前把 body / actions 都强制成 `flex: 1 1 100%`（整行），
     于是「标签 / 内容 / 操作」各占一行，一张卡就要 3 行。
     实测资产库 390 视口：执行进度 112px + 批量生成 180px = 292px，
     加 tabs 42px 与间距，首屏 525px 才开始出现资产卡。
     改为「内容参与行内排布、操作靠右并在放不下时自然折行」后，
     两张卡各压到约 86px（合计 −120px）。 */
  .stage-card {
    padding: var(--sp-2) var(--sp-3);
    margin-bottom: var(--sp-2);
  }
  .stage-row {
    flex-wrap: wrap;
    gap: var(--sp-2);
    row-gap: 6px;
  }
  /* 标签退为行内小标，不再占一整列，也不再独占一行 */
  .stage-label {
    flex: 0 0 auto;
    font-size: 11px;
    line-height: 18px;
    letter-spacing: 0.04em;
  }
  /* 内容不再强制整行：数字/进度条与标签同排，放不下时自身折行 */
  .stage-body {
    flex: 1 1 auto;
    min-width: 0;
    order: 0;
    gap: 6px var(--sp-2);
  }
  /* 操作靠右；放不下时折到下一行但依然右对齐，不独占整行宽度 */
  .stage-actions {
    order: 1;
    flex: 0 0 auto;
    margin-left: auto;
    gap: 6px;
  }
  .stage-meta {
    padding-left: 0;
    order: 2;
    margin-top: 6px;
  }
}
</style>
