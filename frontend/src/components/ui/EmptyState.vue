<script setup lang="ts">
/** 空状态：一句话说明「为什么空」+ 一个明确的下一步 */
withDefaults(
  defineProps<{
    title: string
    description?: string
    actionText?: string
    compact?: boolean
  }>(),
  { description: '', actionText: '', compact: false }
)
defineEmits<{ action: [] }>()
</script>

<template>
  <div class="empty" :class="{ compact }">
    <div class="empty-icon" aria-hidden="true">
      <slot name="icon">◍</slot>
    </div>
    <p class="empty-title">{{ title }}</p>
    <p v-if="description" class="empty-desc">{{ description }}</p>
    <button v-if="actionText" type="button" class="empty-action" @click="$emit('action')">
      {{ actionText }}
    </button>
    <slot />
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-12) var(--sp-6);
  text-align: center;
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
}
.empty.compact {
  padding: var(--sp-6) var(--sp-4);
}
.empty-icon {
  font-size: 22px;
  line-height: 1;
  color: var(--text-tertiary);
  margin-bottom: var(--sp-1);
}
.empty-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.empty-desc {
  margin: 0;
  max-width: 46ch;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-secondary);
}
.empty-action {
  margin-top: var(--sp-3);
  padding: 6px 14px;
  font-size: 13px;
  font-family: inherit;
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border: 1px solid var(--brand-primary);
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}
.empty-action:hover {
  background: var(--brand-hover);
  border-color: var(--brand-hover);
}
</style>
