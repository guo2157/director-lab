<script lang="ts">
/** 供父级复用的筛选类型（SFC 的 script setup 不能 export） */
export type StatusFilter = 'all' | 'draft' | 'active' | 'archived'
export type SortKey = 'updated' | 'name'
</script>

<script setup lang="ts">
/** 作品列表筛选条：状态 chips（左）+ 排序切换（右） */
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
        <span v-if="counts && counts[c.value] !== undefined" class="chip-count">{{ counts[c.value] }}</span>
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
  margin-bottom: var(--sp-4);
}
.filter-chips {
  display: flex;
  align-items: center;
  gap: 6px;
}
.filter-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  font-size: 13px;
  line-height: 20px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
  transition:
    border-color 0.2s ease,
    color 0.2s ease,
    background-color 0.2s ease;
}
.filter-chip:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.filter-chip.active {
  border-color: var(--brand-primary);
  color: var(--brand-primary);
  background: var(--brand-soft);
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
.sort-group {
  display: flex;
  align-items: center;
  margin-left: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  overflow: hidden;
}
.sort-btn {
  padding: 4px 14px;
  font-size: 13px;
  line-height: 20px;
  border: none;
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
  transition:
    color 0.2s ease,
    background-color 0.2s ease;
}
.sort-btn:hover {
  color: var(--text-primary);
}
.sort-btn + .sort-btn {
  border-left: 1px solid var(--border-subtle);
}
.sort-btn.active {
  color: var(--brand-primary);
  background: var(--brand-soft);
}
.sort-btn:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: -2px;
}
</style>
