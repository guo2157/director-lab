<script lang="ts">
/** 供父级复用的筛选类型（SFC 的 script setup 不能 export） */
export type StatusFilter = 'all' | 'draft' | 'active' | 'archived'
</script>

<script setup lang="ts">
/**
 * 作品列表状态筛选条（2026-11-19 改版）。
 *
 * 从「chips + 排序分段」两轴改为纯筛选条，并挂进页面单行工具栏：
 * 排序轴（最近更新 / 名称）已移除——只有两个选项、默认就是最近更新，
 * 几乎没人切，却占着右侧一整块宽度。固定按 updated_at 倒序，结果与
 * 「最近更新」完全一致，功能无损失。
 *
 * 本组件只负责 chips，不再自带 margin / 全宽布局：父级 `.list-toolbar`
 * 用 flex 把它和搜索框、新建按钮排在同一行。
 */
defineProps<{
  /** 各状态数量，用于在 chips 上给出规模感 */
  counts?: Record<StatusFilter, number>
}>()

const statusFilter = defineModel<StatusFilter>('statusFilter', { required: true })

const FILTER_CHIPS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'draft', label: '草稿' },
  { value: 'active', label: '进行中' },
  { value: 'archived', label: '已归档' },
]
</script>

<template>
  <div class="filter-chips" role="group" aria-label="按状态筛选作品">
    <button
      v-for="c in FILTER_CHIPS"
      :key="c.value"
      type="button"
      class="filter-chip"
      :class="{ active: statusFilter === c.value }"
      :aria-pressed="statusFilter === c.value"
      @click="statusFilter = c.value"
    >
      {{ c.label }}
      <span
        v-if="counts && counts[c.value] !== undefined"
        class="chip-count"
        :class="{ 'is-zero': counts[c.value] === 0 }"
      >{{ counts[c.value] }}</span>
    </button>
  </div>
</template>

<style scoped>
.filter-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}

/* 主轴筛选：胶囊；激活 = 品牌 soft 底 + 品牌字 + 内描边 + 加粗（四重编码）
   高度锁 34px，与同排搜索框（n-input medium = 34）等高；新建按钮 36px
   略高一点是有意的（主 CTA 稍大，视觉权重高于筛选），三者基线由
   .list-toolbar 的 align-items: center 对齐。 */
.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 13px;
  font-family: inherit;
  font-size: 13px;
  line-height: 20px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
  transition:
    border-color var(--transition-fast),
    color var(--transition-fast),
    background-color var(--transition-fast);
}
.filter-chip:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
  background: var(--bg-elevated);
}
.filter-chip.active {
  border-color: transparent;
  color: var(--brand-primary);
  background: var(--brand-soft);
  box-shadow: inset 0 0 0 1px var(--brand-line);
  font-weight: 600;
}
.filter-chip:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}

.chip-count {
  font-size: 11px;
  line-height: 16px;
  padding: 0 5px;
  border-radius: var(--radius-pill);
  background: var(--overlay-hover);
  color: inherit;
  font-variant-numeric: tabular-nums;
}
/* 计数为 0 降权：不让「已归档 0」看起来和「进行中 3」同等重要 */
.chip-count.is-zero {
  opacity: 0.45;
}

/* 窄屏：chips 单行横向滚动，不换行。
   换行会把整条工具栏撑到 3 行（4 个 chip + 计数在 390 视口要 2 行）。
   滚动条隐藏，靠右缘裁切提示还有内容 —— 与 JobsView 筛选轴的既有做法一致。 */
@media (max-width: 768px) {
  .filter-chips {
    flex-wrap: nowrap;
    overflow-x: auto;
    overflow-y: hidden;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .filter-chips::-webkit-scrollbar {
    display: none;
  }
}
</style>
