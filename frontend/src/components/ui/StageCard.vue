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

/* 左侧标签列：固定宽度，两张卡靠它对齐成同一套系统。
 *
 * 列宽演进（实测 1440×900）：
 *   60px  最初值。「批量出视频」5 字实测 64px，直接撑破 → 同页两卡内容左边缘差 4px
 *   68px  第一轮修正（12px/500）
 *   76px  第二轮（13px/600）
 *   84px  本轮（14px/600）。14px 下 5 字实测 79px，留 5px 余量
 *
 * 列宽必须跟着字号走：标签是 nowrap 的固定列，字号变大而不加宽列，
 * 文字会溢出到内容区，视觉上「标签压住了工作流下拉框」。
 */
.stage-label {
  flex: 0 0 84px;
  /* 12px/400 → 14px/600，颜色 tertiary → primary（2026-11-21 两轮用户反馈）。
   *
   * 第一轮只提到 13px/600、颜色仍是 --text-secondary，视觉复核结论是
   * 「靠位置能找到，但靠层级找不到」—— 旁边的数值是主色白 + 16px 加粗，
   * 进度条是满格绿，按钮是品牌青，标签反而是全卡最弱的一档，
   * 阅读顺序变成「先看数字，再回头找这是哪张卡」。
   *
   * 这一轮把三件事同时做对：
   *   字号 14px  与内容区数值同档，不再靠 1px 差去区分层级
   *   字重 600   与数值的 400 拉开，标签比内容更「实」
   *   颜色 primary 让标签真正成为卡头，而不是灰色脚注
   * 字距 0.02em：宽字距是「eyebrow 小标」语汇，这里已是固定列，
   * 加大字号后再加宽字距会让 5 字标签继续膨胀。 */
  font-size: 14px;
  font-weight: 600;
  line-height: 24px;
  letter-spacing: 0.02em;
  color: var(--text-primary);
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
  padding-left: calc(84px + var(--sp-4));
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
     改为「标签 + 内容共享第一行、操作靠右」后两张卡各压到 76 / 104px。 */
  .stage-card {
    padding: var(--sp-2) var(--sp-3);
    margin-bottom: var(--sp-2);
  }
  .stage-row {
    flex-wrap: wrap;
    gap: var(--sp-2);
    row-gap: 6px;
  }
  /* 标签退为行内小标，不再占一整列，也不再独占一行。
   * 窄屏 11px/400 → 13px/600：用户反馈的「字体稍大点加粗」在 390 视口同样适用，
   * 而 11px/400 在那个尺寸下几乎读不出「这是个分区标签」。
   * 13px 是能保住「标签 + 内容 + 操作」三者同排的上限 —— 14px 时实测
   * 「批量出视频」占 61px 且 body 被挤到第二行，卡高从 74px 涨到 104px。 */
  .stage-label {
    flex: 0 0 auto;
    font-size: 13px;
    font-weight: 600;
    line-height: 18px;
    letter-spacing: 0.02em;
  }
  /* 内容必须让出「标签宽度」：`flex: 1 1 auto` 的基准是内容宽度，
     而 body 内是可折行的一排控件，基准往往等于整行宽，于是标签单独占第一行、
     body 独占第二行、操作再被挤到第三行（实测批量卡 104px / rowH 86px）。
     显式 `flex: 1 1 0` + `min-width: 0` 后 body 只吃「标签之外」的剩余宽度，
     与标签真正同排，操作也就能留在第一行右侧。 */
  .stage-body {
    flex: 1 1 0;
    min-width: 0;
    order: 0;
    gap: 6px var(--sp-2);
  }
  /* 操作区独占第二行，右对齐。
   *
   * 改前 `flex: 0 0 auto` + 与 label/body 同排：390 视口下三个按钮合计 185px，
   * 加上标签 53px 与间距，body 只剩 78px —— 工作流下拉框被压到 78px 宽，
   * 里面只能显示 2 个字，看起来像坏了。
   *
   * 标签字号从 11px 提到 13px 后这个挤压更明显（body 又少了 6px）。
   * 窄屏本来就没有「一行塞下标签 + 内容 + 操作」的宽度，
   * 强行同排只会让内容区不可用。让操作区换行、独占一行，body 拿回整行宽度。
   * 代价是批量卡从 74px 涨到约 103px —— 换来的是下拉框可读、按钮不挤。 */
  .stage-actions {
    order: 1;
    flex: 1 1 100%;
    margin-left: 0;
    justify-content: flex-end;
    gap: 6px;
  }
  .stage-meta {
    padding-left: 0;
    order: 2;
    margin-top: 6px;
  }
}
</style>
