<script setup lang="ts">
/**
 * 剧本阅读视图：把 Markdown/标准剧本格式排版成可读的分场文档。
 *
 * 版心宽约 46em（中文 700–780px），行高 1.9；集标题分隔、场次头带场号徽标、
 * 场景/人物为元信息行、动作段落弱化、台词角色名与正文悬挂对齐。
 * 只读视图，编辑仍回到「编辑」模式（原文一个字都不改）。
 */
import { computed } from 'vue'
import ScriptLine from './ScriptLine.vue'
import { parseScript } from './scriptParse'

const props = defineProps<{ text: string }>()
const emit = defineEmits<{ edit: [] }>()

const nodes = computed(() => parseScript(props.text))
const sceneCount = computed(() => nodes.value.filter((n) => n.kind === 'scene').length)

function characters(value: string): string[] {
  return value
    .split(/[、,，/／]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}
</script>

<template>
  <div class="reader">
    <div v-if="!nodes.length" class="re-empty">
      <p class="re-empty-title">还没有剧本正文</p>
      <p class="re-empty-hint">
        在「编辑」里粘贴 Markdown 剧本，或只写一句概要点『校验补充』，由 AI 整理成标准剧本格式。
      </p>
      <button type="button" class="re-empty-btn" @click="emit('edit')">去编辑</button>
    </div>

    <article v-else class="re-doc">
      <p v-if="sceneCount" class="re-doc-meta">{{ sceneCount }} 场</p>

      <template v-for="(n, i) in nodes" :key="i">
        <h1 v-if="n.kind === 'episode'" class="re-episode">{{ n.text }}</h1>

        <section v-else-if="n.kind === 'scene'" class="re-scene">
          <header class="re-scene-head">
            <span class="re-scene-no">场次 {{ n.no }}</span>
            <h2 class="re-scene-title">{{ n.title || '未命名场次' }}</h2>
          </header>

          <div v-if="n.meta.length" class="re-meta">
            <div v-for="m in n.meta" :key="m.label" class="re-meta-item">
              <span class="re-meta-k">{{ m.label }}</span>
              <span v-if="m.label === '人物'" class="re-chars">
                <span v-for="c in characters(m.value)" :key="c" class="re-char">{{ c }}</span>
              </span>
              <span v-else class="re-meta-v">{{ m.value }}</span>
            </div>
          </div>

          <div class="re-scene-body">
            <ScriptLine v-for="(b, j) in n.body" :key="j" :block="b" />
          </div>
        </section>

        <div v-else-if="n.kind === 'loose'" class="re-loose">
          <ScriptLine v-for="(b, j) in n.body" :key="j" :block="b" />
        </div>

        <p v-else class="re-end">{{ n.text }}</p>
      </template>
    </article>
  </div>
</template>

<style scoped>
.reader {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  background: var(--bg-base);
}

/* 版心：中文剧本一行 36–42 字最舒服，约 46em */
.re-doc {
  max-width: 46em;
  margin: 0 auto;
  padding: var(--sp-8) var(--sp-6) var(--sp-16);
}
.re-doc-meta {
  margin: 0 0 var(--sp-4);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
}

.re-episode {
  margin: var(--sp-2) 0 var(--sp-6);
  padding-bottom: var(--sp-3);
  font-family: var(--font-display);
  font-size: 26px;
  font-weight: 600;
  line-height: 1.4;
  letter-spacing: 0.02em;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border-subtle);
}

/* 场次：场号徽标 + 标题，正文用一条竖线归组 */
.re-scene {
  padding: var(--sp-4) 0 var(--sp-5);
}
.re-scene + .re-scene {
  border-top: 1px solid var(--border-subtle);
}
.re-scene-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-bottom: var(--sp-2);
}
.re-scene-no {
  flex: none;
  padding: 0 var(--sp-2);
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 20px;
  color: var(--brand-hover);
  background: var(--brand-soft);
  border: 1px solid var(--brand-line);
  border-radius: var(--radius-sm);
}
.re-scene-title {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
}

.re-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0 0 var(--sp-3);
  padding: 0 0 0 2px;
}
.re-meta-item {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: 12.5px;
  line-height: 1.7;
}
.re-meta-k {
  flex: none;
  width: 2.6em;
  font-size: 11px;
  color: var(--text-tertiary);
}
.re-meta-v {
  color: var(--text-secondary);
}
.re-chars {
  display: inline-flex;
  gap: 6px;
  flex-wrap: wrap;
}
.re-char {
  padding: 0 var(--sp-2);
  font-size: 11.5px;
  line-height: 19px;
  color: var(--text-secondary);
  background: var(--neutral-soft);
  border: 1px solid var(--neutral-line);
  border-radius: var(--radius-sm);
}

.re-scene-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding-left: var(--sp-4);
  border-left: 2px solid var(--border-subtle);
}

.re-loose {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.re-end {
  margin: var(--sp-8) 0 var(--sp-4);
  font-size: 12px;
  letter-spacing: 0.12em;
  text-align: center;
  color: var(--text-tertiary);
}

/* 空状态 */
.re-empty {
  max-width: 34em;
  margin: 0 auto;
  padding: var(--sp-16) var(--sp-6);
  text-align: center;
}
.re-empty-title {
  margin: 0 0 var(--sp-2);
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.re-empty-hint {
  margin: 0 0 var(--sp-4);
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-tertiary);
}
.re-empty-btn {
  padding: 4px var(--sp-4);
  font-family: inherit;
  font-size: 13px;
  line-height: 22px;
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border: 1px solid var(--brand-primary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-fast);
}
.re-empty-btn:hover {
  background: var(--brand-hover);
}
.re-empty-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

@media (max-width: 768px) {
  .re-doc {
    padding: var(--sp-5) var(--sp-4) var(--sp-12);
  }
  .re-episode {
    font-size: 22px;
  }
  .re-scene-body {
    padding-left: var(--sp-3);
  }
}
</style>
