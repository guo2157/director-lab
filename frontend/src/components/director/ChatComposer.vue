<script setup lang="ts">
/**
 * 消息输入区：多行输入 + 内嵌发送按钮。
 * 键盘可访问：输入框与按钮均有 aria-label，焦点态沿用 --focus-ring 语义（不写死颜色）。
 */
import { NButton, NInput } from 'naive-ui'

const props = withDefaults(
  defineProps<{
    modelValue: string
    disabled?: boolean
    loading?: boolean
    placeholder?: string
  }>(),
  {
    disabled: false,
    loading: false,
    placeholder: '输入消息，Enter 发送（Shift+Enter 换行）',
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  send: []
}>()

const canSend = () => !props.disabled && !props.loading && props.modelValue.trim().length > 0
</script>

<template>
  <div class="chat-input">
    <div class="composer">
      <NInput
        :value="modelValue"
        type="textarea"
        :rows="2"
        :placeholder="placeholder"
        :disabled="disabled"
        :input-props="{ 'aria-label': '消息输入框' }"
        @update:value="(v: string) => emit('update:modelValue', v)"
        @keydown.enter.exact.prevent="emit('send')"
      />
      <NButton
        class="composer-send"
        type="primary"
        aria-label="发送消息"
        :loading="loading"
        :disabled="!canSend()"
        @click="emit('send')"
      >
        发送
      </NButton>
    </div>
  </div>
</template>

<style scoped>
.chat-input {
  display: block;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  border-top: 1px solid var(--border-subtle);
  flex-shrink: 0;
  background: var(--bg-panel);
}

/* 输入框与发送按钮合为一体：按钮内嵌在输入框右下角，不再单独占一行 */
.composer {
  position: relative;
  display: block;
}

.composer :deep(.n-input) {
  border-radius: var(--radius-lg);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.composer :deep(.n-input .n-input__textarea) {
  font-size: 14px;
  line-height: 1.7;
}

/* 让输入文字避开右下角的发送按钮，避免被遮挡 */
.composer :deep(.n-input .n-input__textarea-el) {
  padding-right: 92px;
}

.composer :deep(.n-input .n-input__textarea-el:focus-visible) {
  outline: 2px solid var(--brand-hover);
  outline-offset: -2px;
  border-radius: var(--radius);
}

.composer-send {
  position: absolute;
  right: var(--sp-2);
  bottom: var(--sp-2);
  z-index: 2;
  border-radius: var(--radius) !important;
  height: 32px !important;
  min-width: 58px !important;
  font-weight: 500;
  transition: background var(--transition-fast);
}

.composer-send:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}

@media (max-width: 768px) {
  .chat-input {
    padding: var(--sp-2) var(--sp-3) var(--sp-3);
  }
  .composer :deep(.n-input .n-input__textarea-el) {
    padding-right: 82px;
  }
}
</style>
