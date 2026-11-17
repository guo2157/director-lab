<script setup lang="ts">
/**
 * 提示词编辑块：默认折叠为 3 行摘要，展开才可编辑；
 * 提供复制、恢复默认、字数提示。长提示词不再平铺占满页面。
 */
import { computed, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: string
    label?: string
    /** 默认模板：用于「恢复默认」与「当前是否默认模板」判断 */
    defaultText?: string
    rows?: number
    placeholder?: string
    /** 收起时的可见行数 */
    collapsedLines?: number
    disabled?: boolean
  }>(),
  {
    label: '提示词',
    defaultText: '',
    rows: 8,
    placeholder: '尚未生成提示词',
    collapsedLines: 3,
    disabled: false
  }
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  copy: [value: string]
  reset: [value: string]
}>()

const expanded = ref(false)
const charCount = computed(() => props.modelValue.length)
const isDefault = computed(() => !!props.defaultText && props.modelValue === props.defaultText)
const isEmpty = computed(() => !props.modelValue.trim())

function toggle() {
  expanded.value = !expanded.value
}

async function handleCopy() {
  try {
    await navigator.clipboard.writeText(props.modelValue)
  } catch {
    /* 剪贴板不可用时静默失败，由父组件给出提示 */
  }
  emit('copy', props.modelValue)
}

function handleReset() {
  emit('update:modelValue', props.defaultText)
  emit('reset', props.defaultText)
}
</script>

<template>
  <section class="prompt-editor">
    <header class="pe-head">
      <span class="pe-label">{{ label }}</span>
      <span class="pe-count">{{ charCount }} 字</span>
      <span v-if="isDefault" class="pe-badge">默认模板</span>
      <span class="pe-spacer" />
      <button type="button" class="pe-btn" :disabled="isEmpty" @click="handleCopy">复制</button>
      <button
        v-if="defaultText && !isDefault"
        type="button"
        class="pe-btn"
        :disabled="disabled"
        @click="handleReset"
      >
        恢复默认
      </button>
      <button type="button" class="pe-btn" :aria-expanded="expanded" @click="toggle">
        {{ expanded ? '收起' : '展开编辑' }}
      </button>
    </header>

    <p
      v-if="!expanded"
      class="pe-preview"
      :style="{ '-webkit-line-clamp': collapsedLines }"
      :class="{ 'is-empty': isEmpty }"
      @click="toggle"
    >
      {{ isEmpty ? placeholder : modelValue }}
    </p>

    <textarea
      v-else
      class="pe-input"
      :value="modelValue"
      :rows="rows"
      :placeholder="placeholder"
      :disabled="disabled"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    />
  </section>
</template>

<style scoped>
.prompt-editor {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.pe-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: 12px;
  color: var(--text-secondary);
}
.pe-label {
  font-weight: 600;
  color: var(--text-primary);
}
.pe-count {
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}
.pe-badge {
  padding: 0 6px;
  line-height: 18px;
  border-radius: var(--radius-sm);
  color: var(--text-tertiary);
  background: var(--muted-soft);
  border: 1px solid var(--muted-line);
}
.pe-spacer {
  flex: 1;
}
.pe-btn {
  padding: 2px 8px;
  font-size: 12px;
  font-family: inherit;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.pe-btn:hover:not(:disabled) {
  color: var(--brand-hover);
  border-color: var(--brand-line);
}
.pe-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.pe-preview {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  overflow: hidden;
  cursor: pointer;
}
.pe-preview.is-empty {
  color: var(--text-tertiary);
  font-family: var(--font-ui);
}
.pe-input {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  font-size: 13px;
  line-height: 1.6;
  font-family: var(--font-mono);
  color: var(--text-primary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  resize: vertical;
}
.pe-input:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
  border-color: var(--brand-hover);
}
</style>
