<script setup lang="ts">
/**
 * 页面头部：标题 + 副标题 + 状态 + 主操作。
 * 每个步骤页只有一个主按钮（slot actions 里的第一个），其余为次要按钮。
 */
withDefaults(
  defineProps<{
    title: string
    subtitle?: string
    /** 顶部小标签，如「第 1 集 / 已完成」 */
    eyebrow?: string
    /** 无操作区时是否仍保留底部分隔线 */
    divider?: boolean
  }>(),
  { subtitle: '', eyebrow: '', divider: true }
)
</script>

<template>
  <header class="page-header" :class="{ 'no-divider': !divider }">
    <div class="header-left">
      <div class="title-block">
        <span v-if="eyebrow" class="eyebrow">{{ eyebrow }}</span>
        <h1 class="page-title">{{ title }}</h1>
        <p v-if="subtitle || $slots.subtitle" class="page-subtitle">
          <slot name="subtitle">{{ subtitle }}</slot>
        </p>
      </div>
      <div v-if="$slots.status" class="status-slot">
        <slot name="status" />
      </div>
    </div>
    <div v-if="$slots.actions" class="action-slot">
      <slot name="actions" />
    </div>
    <slot />
  </header>
</template>

<style scoped>
.page-header {
  align-items: flex-start;
}
.page-header.no-divider {
  border-bottom: none;
  padding-bottom: 0;
}
.title-block {
  min-width: 0;
}
.eyebrow {
  display: block;
  font-size: 12px;
  line-height: 19px;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
  margin-bottom: 2px;
}
.status-slot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding-bottom: 3px;
}
.action-slot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  justify-content: flex-end;
}
</style>
