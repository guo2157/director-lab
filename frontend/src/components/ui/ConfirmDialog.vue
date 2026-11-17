<script setup lang="ts">
/** 危险操作确认：必须展示影响范围，而不是只问「确定吗」 */
import { NModal } from 'naive-ui'

withDefaults(
  defineProps<{
    show: boolean
    title: string
    message?: string
    /** 影响范围：逐条列出生效对象 */
    impact?: string[]
    confirmText?: string
    cancelText?: string
    danger?: boolean
    loading?: boolean
    /** 隐藏取消按钮（纯告知场景，如「知道了」） */
    hideCancel?: boolean
  }>(),
  {
    message: '',
    impact: () => [],
    confirmText: '确认',
    cancelText: '取消',
    danger: true,
    loading: false,
    hideCancel: false
  }
)

const emit = defineEmits<{ 'update:show': [boolean]; confirm: []; cancel: [] }>()

function close() {
  emit('update:show', false)
  emit('cancel')
}
</script>

<template>
  <n-modal
    :show="show"
    :mask-closable="!loading"
    preset="dialog"
    :title="title"
    :content="undefined"
    @update:show="emit('update:show', $event)"
  >
    <div class="confirm-body">
      <p v-if="message" class="confirm-msg">{{ message }}</p>
      <ul v-if="impact.length" class="confirm-impact">
        <li v-for="(item, i) in impact" :key="i">{{ item }}</li>
      </ul>
    </div>
    <template #action>
      <button v-if="!hideCancel" type="button" class="cd-btn" :disabled="loading" @click="close">
        {{ cancelText }}
      </button>
      <button
        type="button"
        class="cd-btn"
        :class="danger ? 'is-danger' : 'is-primary'"
        :disabled="loading"
        @click="emit('confirm')"
      >
        {{ loading ? '处理中…' : confirmText }}
      </button>
    </template>
  </n-modal>
</template>

<style scoped>
.confirm-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.confirm-msg {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.confirm-impact {
  margin: 0;
  padding: var(--sp-2) var(--sp-3) var(--sp-2) var(--sp-6);
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-primary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.cd-btn {
  padding: 5px 14px;
  font-size: 13px;
  font-family: inherit;
  color: var(--text-primary);
  background: transparent;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}
.cd-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.cd-btn:hover:not(:disabled) {
  border-color: var(--brand-primary);
}
.cd-btn.is-primary {
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border-color: var(--brand-primary);
}
.cd-btn.is-danger {
  color: var(--text-primary);
  background: var(--danger-soft);
  border-color: var(--danger-line);
}
.cd-btn.is-danger:hover:not(:disabled) {
  background: var(--danger);
  color: var(--text-on-brand);
  border-color: var(--danger);
}
</style>
