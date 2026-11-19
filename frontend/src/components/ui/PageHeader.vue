<script setup lang="ts">
/**
 * 页面头部：标题 + 副标题 + 状态 + 主操作。
 * 每个步骤页只有一个主按钮（slot actions 里的第一个），其余为次要按钮。
 *
 * compact 模式（v6，2026-11-19）：
 * 项目内的 5 个步骤页已经在左侧「制作流程」竖列里被高亮，页内再画一遍同名
 * 30px H1 等于同一信息说两遍。compact 模式把 H1 降为 .visually-hidden 保留
 * 语义与文档大纲，页头收敛为单行工具条（左状态 / 右操作），并去掉底部分隔线
 * 与下外边距 —— 因为此时页头不再是「章节标题」，而是「本页操作条」。
 */
withDefaults(
  defineProps<{
    title: string
    subtitle?: string
    /** 顶部小标签，如「第 1 集 / 已完成」 */
    eyebrow?: string
    /** 无操作区时是否仍保留底部分隔线 */
    divider?: boolean
    /** 标题仅保留给读屏：用于左侧流程栏已高亮当前页的步骤页 */
    compact?: boolean
  }>(),
  { subtitle: '', eyebrow: '', divider: true, compact: false }
)
</script>

<template>
  <header class="page-header" :class="{ 'no-divider': !divider, 'is-compact': compact }">
    <div class="header-left">
      <div class="title-block">
        <span v-if="eyebrow && !compact" class="eyebrow">{{ eyebrow }}</span>
        <h1 class="page-title" :class="{ 'visually-hidden': compact }">{{ title }}</h1>
        <p v-if="(subtitle || $slots.subtitle) && !compact" class="page-subtitle">
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

/* 紧凑模式：页头退化为单行操作条 —— 状态在左、操作在右、垂直居中对齐 */
.page-header.is-compact {
  align-items: center;
  flex-wrap: nowrap;
  gap: var(--sp-3);
  padding-bottom: 0;
  margin-bottom: 0;
  border-bottom: none;
  min-height: 32px;
}
.page-header.is-compact .header-left {
  align-items: center;
  gap: var(--sp-3);
  flex: 1 1 auto;
  min-width: 0;
}
/* 紧凑模式下标题块只剩一个 .visually-hidden 的 H1（absolute，不占位），
   不能用 display:none —— 那会把 H1 从无障碍树里一并摘掉 */

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
.page-header.is-compact .status-slot {
  padding-bottom: 0;
  flex-wrap: wrap;
  min-width: 0;
}
.action-slot {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  justify-content: flex-end;
}
.page-header.is-compact .action-slot {
  flex: 0 0 auto;
  flex-wrap: nowrap;
}

@media (max-width: 768px) {
  .page-header.is-compact {
    flex-wrap: wrap;
  }
  .page-header.is-compact .action-slot {
    flex-wrap: wrap;
  }
}
</style>
