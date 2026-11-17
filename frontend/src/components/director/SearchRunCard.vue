<script setup lang="ts">
/**
 * 联网搜索执行卡片：展示真实搜索执行记录（来自后端 payload.search，非 LLM 生成）。
 * 进行中时叠加 SSE 快照的阶段与进度，终态后回落到最终记录。
 */
import { NIcon } from 'naive-ui'
import { SearchOutline } from '@vicons/ionicons5'
import type { SearchRecord } from './helpers'
import { SEARCH_STATUS_TEXT } from './helpers'

withDefaults(
  defineProps<{
    record: SearchRecord
    /** 进行中的实时阶段与进度（null 表示已终态） */
    live?: { phase: string; progress: number } | null
  }>(),
  { live: null },
)
</script>

<template>
  <div class="search-card">
    <div class="search-card-header">
      <span class="search-card-icon" aria-hidden="true"><n-icon :component="SearchOutline" :size="15" /></span>
      <span class="search-card-title">联网搜索</span>
      <span class="search-card-status" :data-status="record.status">
        {{ SEARCH_STATUS_TEXT[record.status] || record.status }}
      </span>
    </div>

    <div v-if="live" class="search-card-live">
      <span class="search-card-live-phase">{{ live.phase }}</span>
      <div class="search-card-live-bar">
        <div class="search-card-live-fill" :style="{ width: `${live.progress}%` }" />
      </div>
      <span class="search-card-live-pct">{{ live.progress }}%</span>
    </div>

    <div class="search-card-meta">
      <span>关键词：{{ record.query }}</span>
      <span v-if="record.provider">引擎：{{ record.provider }}</span>
      <span>有效结果：{{ record.valid_result_count }}/{{ record.result_count }}</span>
      <span v-if="record.duration_ms != null">耗时：{{ record.duration_ms }}ms</span>
    </div>

    <div v-if="record.error" class="search-card-error">{{ record.error }}</div>
  </div>
</template>

<style scoped>
.search-card {
  margin-top: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--neutral-line);
  border-radius: var(--radius-lg);
  background: var(--neutral-soft);
  font-size: 12px;
}

.search-card-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-weight: 600;
}

.search-card-status {
  margin-left: auto;
  padding: 1px var(--sp-2);
  border-radius: var(--radius-sm);
  font-weight: 500;
  font-size: 11px;
  background: var(--success-soft);
  color: var(--state-success);
}

.search-card-status[data-status='partial'] {
  background: var(--warning-soft);
  color: var(--state-warning);
}

.search-card-status[data-status='failed'],
.search-card-status[data-status='empty'] {
  background: var(--danger-soft);
  color: var(--state-danger);
}

.search-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-4);
  margin-top: var(--sp-1);
  color: var(--text-secondary);
}

.search-card-error {
  margin-top: var(--sp-1);
  color: var(--state-danger);
}

.search-card-live {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-1);
  font-size: 12px;
  color: var(--brand-primary);
}

.search-card-live-phase {
  min-width: 56px;
}

.search-card-live-bar {
  flex: 1;
  height: 4px;
  border-radius: var(--radius-sm);
  background: var(--border-subtle);
  overflow: hidden;
}

.search-card-live-fill {
  height: 100%;
  border-radius: var(--radius-sm);
  background: var(--brand-primary);
  transition: width var(--transition-base);
}

.search-card-live-pct {
  font-variant-numeric: tabular-nums;
}
</style>
