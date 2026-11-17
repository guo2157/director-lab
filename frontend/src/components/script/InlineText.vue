<script setup lang="ts">
/** 行内格式渲染：**加粗** / *斜体* / `代码` / @引用。文本来自用户剧本，不用 v-html。 */
import { computed } from 'vue'
import { inlineParts } from './scriptParse'

const props = defineProps<{ text: string }>()
const parts = computed(() => inlineParts(props.text))
</script>

<template>
  <template v-for="(p, i) in parts" :key="i">
    <strong v-if="p.cls === 'b'">{{ p.text }}</strong>
    <em v-else-if="p.cls === 'i'">{{ p.text }}</em>
    <code v-else-if="p.cls === 'code'" class="inl-code">{{ p.text }}</code>
    <span v-else-if="p.cls === 'at'" class="inl-at">{{ p.text }}</span>
    <template v-else>{{ p.text }}</template>
  </template>
</template>

<style scoped>
.inl-code {
  padding: 0 4px;
  font-family: var(--font-mono);
  font-size: 0.92em;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
.inl-at {
  color: var(--brand-hover);
  font-weight: 500;
}
strong {
  color: var(--text-primary);
  font-weight: 600;
}
em {
  color: var(--text-secondary);
}
</style>
