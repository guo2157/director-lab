<script setup lang="ts">
/**
 * 消息输入区：多行输入 + 内嵌发送按钮。
 * 键盘可访问：输入框与按钮均有 aria-label，焦点态沿用 --focus-ring 语义（不写死颜色）。
 */
import { ref } from 'vue'
import { NButton, NInput } from 'naive-ui'

const inputRef = ref<{ focus: () => void } | null>(null)
/** 供父级（快捷提示词）把焦点交还输入框 */
defineExpose({ focus: () => inputRef.value?.focus() })

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
    placeholder: '描述你想推进的创作任务…',
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
        ref="inputRef"
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
    <div class="composer-foot">
      <span v-if="disabled && !loading" class="composer-hint">选择或新建会话后即可输入</span>
      <span v-else class="composer-hint">
        <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行
      </span>
    </div>
  </div>
</template>

<style scoped>
.chat-input {
  display: block;
  padding: var(--sp-3) var(--sp-4) var(--sp-2);
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

/* 聚焦：组件自身边框提亮 + 一圈极淡品牌光晕，替代被移除的内嵌 outline */
.composer :deep(.n-input.n-input--focus) {
  border-color: var(--brand-primary);
  box-shadow: 0 0 0 3px var(--brand-glow);
}

.composer :deep(.n-input .n-input__textarea) {
  font-size: 14px;
  line-height: 1.7;
}

/* 让输入文字避开右下角的发送按钮，避免被遮挡 */
.composer :deep(.n-input .n-input__textarea-el) {
  padding-right: 92px;
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

/* 键位提示：说明「Enter 发送」，不占用输入区视觉重心 */
.composer-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-2);
  min-height: 18px;
  padding: var(--sp-1) var(--sp-1) 0;
}

.composer-hint {
  font-size: 11px;
  line-height: 16px;
  color: var(--text-tertiary);
}

.composer-hint kbd {
  padding: 0 4px;
  font-family: var(--font-ui);
  font-size: 10px;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}

@media (max-width: 768px) {
  .chat-input {
    padding: var(--sp-2) var(--sp-3) var(--sp-2);
  }
  .composer :deep(.n-input .n-input__textarea-el) {
    padding-right: 82px;
  }
  .composer-foot {
    display: none;
  }
}
</style>
