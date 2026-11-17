<script setup lang="ts">
/** 四步流程条：初始剧本 → 校验补充 → 保存版本 → 开始分析。纯展示。 */
type SaveVersion = { version: number } | null

defineProps<{
  steps: ReadonlyArray<{ key: string; label: string }>
  /** 当前阶段下标；之前的视为已完成 */
  index: number
  savedVersion: SaveVersion
  hint: string
}>()
</script>

<template>
  <div class="stage-bar">
    <div
      v-for="(s, i) in steps"
      :key="s.key"
      class="stage-item"
      :class="{ active: index === i, done: index > i }"
    >
      <span class="stage-index">{{ index > i ? '✓' : i + 1 }}</span>
      <span class="stage-label">{{ s.label }}</span>
      <span v-if="s.key === 'saved' && savedVersion" class="stage-extra">v{{ savedVersion.version }}</span>
    </div>
    <span class="stage-hint">{{ hint }}</span>
  </div>
</template>

<style scoped>
.stage-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.stage-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-tertiary);
}
.stage-item.active {
  color: var(--brand-primary);
  font-weight: 600;
}
.stage-item.done {
  color: var(--state-success);
}
.stage-index {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  border: 1px solid currentColor;
  flex-shrink: 0;
}
.stage-extra {
  font-size: 11px;
  color: var(--text-secondary);
}
.stage-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-tertiary);
}

@media (max-width: 768px) {
  .stage-hint {
    display: none;
  }
}
</style>
