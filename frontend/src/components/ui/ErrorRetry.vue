<script setup lang="ts">
/** 错误重试块：说明发生了什么 + 可操作的下一步，不只抛错误码 */
withDefaults(
  defineProps<{
    title?: string
    message?: string
    retryText?: string
    compact?: boolean
  }>(),
  { title: '加载失败', message: '', retryText: '重试', compact: false }
)
defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="error-block" :class="{ compact }" role="alert">
    <div class="error-head">
      <span class="error-mark" aria-hidden="true">!</span>
      <p class="error-title">{{ title }}</p>
    </div>
    <p v-if="message" class="error-msg">{{ message }}</p>
    <slot />
    <button type="button" class="error-retry" @click="$emit('retry')">{{ retryText }}</button>
  </div>
</template>

<style scoped>
.error-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-5);
  border: 1px solid var(--danger-line);
  border-left-width: 2px;
  border-radius: var(--radius-lg);
  background: var(--danger-soft);
}
.error-block.compact {
  padding: var(--sp-3) var(--sp-4);
}
.error-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.error-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  font-size: 11px;
  font-weight: 700;
  color: var(--text-on-brand);
  background: var(--danger);
  border-radius: 50%;
  flex: none;
}
.error-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.error-msg {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-secondary);
  word-break: break-word;
}
.error-retry {
  align-self: flex-start;
  margin-top: var(--sp-1);
  padding: 5px 12px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text-primary);
  background: transparent;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color var(--transition-fast), color var(--transition-fast);
}
.error-retry:hover {
  border-color: var(--brand-primary);
  color: var(--brand-hover);
}
</style>
