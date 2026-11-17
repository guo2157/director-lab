<script setup lang="ts">
/** 剧本工具条：视图切换（编辑/阅读/分栏）+ 字数。三种模式下都常驻，不会把切换入口藏掉。 */
type ScriptViewMode = 'edit' | 'read' | 'split'

const props = defineProps<{
  viewMode: ScriptViewMode
  modes: Array<{ value: ScriptViewMode; label: string }>
  /** 窗口是否够宽（决定分栏可用） */
  wide: boolean
  chars: number
  maxChars: number
}>()

const emit = defineEmits<{ 'update:viewMode': [ScriptViewMode] }>()

function valid(): boolean {
  return props.chars >= 1 && props.chars <= props.maxChars
}
</script>

<template>
  <div class="pane-toolbar">
    <span class="pane-label">Markdown 剧本</span>
    <div class="view-switch" role="tablist" aria-label="剧本视图">
      <button
        v-for="m in modes"
        :key="m.value"
        type="button"
        role="tab"
        :aria-selected="viewMode === m.value"
        :class="{ active: viewMode === m.value }"
        :disabled="m.value === 'split' && !wide"
        :title="m.value === 'split' && !wide ? '窗口宽度不足 1080px，暂不可分栏' : ''"
        @click="emit('update:viewMode', m.value)"
      >
        {{ m.label }}
      </button>
    </div>
    <span class="toolbar-gap" />
    <span class="char-count" :class="{ over: !valid() }">{{ chars }} / {{ maxChars }}</span>
  </div>
</template>

<style scoped>
.pane-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: 6px var(--sp-4);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  flex-shrink: 0;
}
.pane-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.view-switch {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.view-switch button {
  padding: 0 var(--sp-3);
  font-family: inherit;
  font-size: 12px;
  line-height: 20px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
}
.view-switch button:hover:not(:disabled) {
  color: var(--text-primary);
}
.view-switch button.active {
  color: var(--text-primary);
  background: var(--bg-elevated);
  border-color: var(--border-subtle);
}
.view-switch button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.view-switch button:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.toolbar-gap {
  flex: 1;
  min-width: var(--sp-2);
}
.char-count {
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.char-count.over {
  color: var(--brand-error);
}

@media (max-width: 768px) {
  .pane-toolbar {
    flex-wrap: wrap;
    gap: var(--sp-2);
  }
}
</style>
