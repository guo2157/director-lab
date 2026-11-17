<script setup lang="ts">
/**
 * 单个剧本块：动作 / 台词 / 标题 / 列表 / 引用 / 分隔线 / 普通段落。
 * 台词用「角色名 + 台词」悬挂对齐，动作按正文段落排版——不再靠 △ 和冒号硬分。
 */
import { computed } from 'vue'
import InlineText from './InlineText.vue'
import type { Block } from './scriptParse'

const props = defineProps<{ block: Block }>()

const headingTag = computed(() => {
  const level = Math.min(Math.max(props.block.level ?? 3, 2), 4)
  return `h${level}`
})
const listItems = computed(() => props.block.items ?? [])
</script>

<template>
  <p v-if="block.kind === 'action'" class="ln is-action"><InlineText :text="block.text" /></p>

  <div v-else-if="block.kind === 'dialogue'" class="ln is-dialogue">
    <span class="ln-who">{{ block.who }}</span>
    <p class="ln-say"><InlineText :text="block.text" /></p>
  </div>

  <component :is="headingTag" v-else-if="block.kind === 'heading'" class="ln is-heading">
    <InlineText :text="block.text" />
  </component>

  <ul v-else-if="block.kind === 'list'" class="ln is-list">
    <li v-for="(item, i) in listItems" :key="i"><InlineText :text="item" /></li>
  </ul>

  <blockquote v-else-if="block.kind === 'quote'" class="ln is-quote">
    <InlineText :text="block.text" />
  </blockquote>

  <div v-else-if="block.kind === 'divider'" class="ln is-divider" role="separator" />

  <p v-else class="ln is-para"><InlineText :text="block.text" /></p>
</template>

<style scoped>
.ln {
  margin: 0;
  font-size: 14.5px;
  line-height: 1.9;
  color: var(--text-primary);
  word-break: break-word;
}

/* 动作/场景描述：正文色减弱一档，读起来不抢台词的注意力 */
.is-action {
  color: var(--text-secondary);
}
.is-para {
  color: var(--text-secondary);
}

/* 台词：角色名固定左栏，台词悬挂对齐，长台词换行不回到角色名下 */
.is-dialogue {
  display: grid;
  grid-template-columns: 3.8em minmax(0, 1fr);
  gap: var(--sp-2);
  align-items: baseline;
  padding: 2px 0;
}
.ln-who {
  font-size: 13px;
  font-weight: 600;
  color: var(--brand-hover);
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ln-say {
  margin: 0;
  font-size: 15px;
  line-height: 1.85;
  color: var(--text-primary);
}

.is-heading {
  margin: var(--sp-3) 0 0;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.6;
  color: var(--text-primary);
}
.is-list {
  margin: 0;
  padding-left: 1.3em;
  color: var(--text-secondary);
}
.is-list li {
  margin: 2px 0;
}
.is-quote {
  margin: 0;
  padding: 2px var(--sp-3);
  border-left: 2px solid var(--brand-line);
  color: var(--text-secondary);
}
.is-divider {
  height: 1px;
  margin: var(--sp-3) 0;
  background: var(--border-subtle);
}

@media (max-width: 768px) {
  .is-dialogue {
    grid-template-columns: 3.2em minmax(0, 1fr);
    gap: 6px;
  }
  .ln-say {
    font-size: 14.5px;
  }
}
</style>
