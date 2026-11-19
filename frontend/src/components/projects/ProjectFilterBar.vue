<script lang="ts">
/** 供父级复用的筛选类型（SFC 的 script setup 不能 export） */
export type StatusFilter = 'all' | 'draft' | 'active' | 'archived'
export type SortKey = 'updated' | 'name'
</script>

<script setup lang="ts">
/** 作品列表筛选条：状态 chips（主轴，品牌色）+ 排序切换（次轴，中性色） */
defineProps<{
  /** 各状态数量，用于在 chips 上给出规模感 */
  counts?: Record<StatusFilter, number>
}>()

const statusFilter = defineModel<StatusFilter>('statusFilter', { required: true })
const sortKey = defineModel<SortKey>('sortKey', { required: true })

const FILTER_CHIPS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'draft', label: '草稿' },
  { value: 'active', label: '进行中' },
  { value: 'archived', label: '已归档' },
]
const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: 'updated', label: '最近更新' },
  { value: 'name', label: '名称' },
]
</script>

<template>
  <div class="toolbar">
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

    <div class="sort-group" role="group" aria-label="排序方式">
      <button
        v-for="s in SORT_OPTIONS"
        :key="s.value"
        type="button"
        class="sort-btn"
        :class="{ active: sortKey === s.value }"
        :aria-pressed="sortKey === s.value"
        @click="sortKey = s.value"
      >
        {{ s.label }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
  margin-bottom: var(--sp-5);
}
.filter-chips {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

/* 主轴筛选：胶囊；激活 = 品牌 soft 底 + 品牌字 + 内描边 + 加粗（四重编码） */
.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 13px;
  font-family: inherit;
  font-size: 13px;
  line-height: 20px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
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

/* 次轴排序：分段控件，激活走中性底 —— 与主轴品牌色分级，避免两个「当前选中」抢主次 */
.sort-group {
  display: flex;
  align-items: center;
  margin-left: auto;
  padding: 2px;
  gap: 2px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  background: var(--bg-panel);
}
.sort-btn {
  padding: 3px 13px;
  font-family: inherit;
  font-size: 12.5px;
  line-height: 20px;
  border: none;
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast);
}
.sort-btn:hover {
  color: var(--text-primary);
}
.sort-btn.active {
  color: var(--text-primary);
  background: var(--neutral-soft);
  box-shadow: inset 0 0 0 1px var(--border-strong);
  font-weight: 500;
}
.sort-btn:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}

@media (max-width: 480px) {
  .sort-group {
    margin-left: 0;
    width: 100%;
  }
  .sort-btn {
    flex: 1;
    text-align: center;
  }
}
</style>
