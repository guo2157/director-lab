<script setup lang="ts">
/**
 * 提示词面板：结构化展示 + 就地编辑。
 *
 * 后端 compile_storyboard_image 产出【任务】【参考图职责】【美术风格】【目标画面】
 * 【必须保持】【允许变化】【禁止】【输出】等段落：
 *   · 【任务】【参考图职责】收进头部元信息（任务名 / 参考图张数 / 字数），不重复列出；
 *   · 【目标画面】作为正文完整展示，不折叠、不截断；
 *   · 其余段落逐条列出全文——信息不藏进折叠区，要改时才切换成编辑态。
 */
import { computed, ref } from 'vue'
import { splitPromptSegments } from './promptSummary'

const props = withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
  }>(),
  { placeholder: '尚未生成分镜图提示词' },
)

const emit = defineEmits<{
  'update:modelValue': [string]
  copied: []
}>()

/** 头部元信息已覆盖的段落 */
const META_KEYS = ['任务', '参考图职责']
/** 作为正文展示的段落 */
const MAIN_KEYS = ['目标画面', '正文', '画面']

const editing = ref(false)
const segments = computed(() => splitPromptSegments(props.modelValue))
const task = computed(() => (segments.value['任务'] || '').replace(/\s+/g, ' '))
const body = computed(() => MAIN_KEYS.map((k) => segments.value[k]).find((v) => !!v) || '')
const refCount = computed(() => ((segments.value['参考图职责'] || '').match(/图\s*\d+\s*[：:]/g) || []).length)
const rest = computed(() =>
  Object.entries(segments.value)
    .filter(([k]) => !META_KEYS.includes(k) && !MAIN_KEYS.includes(k))
    .map(([name, text]) => ({ name, text })),
)
const chars = computed(() => props.modelValue.trim().length)
const isEmpty = computed(() => !props.modelValue.trim())
/** 无【】段落的自由文本：整体当正文 */
const plainBody = computed(() => (rest.value.length || body.value ? '' : props.modelValue.trim()))

async function copy() {
  try {
    await navigator.clipboard.writeText(props.modelValue)
  } catch {
    /* 剪贴板不可用时静默，由父组件提示 */
  }
  emit('copied')
}
</script>

<template>
  <div class="pp">
    <header class="pp-head">
      <span v-if="task" class="pp-task" :title="task">{{ task }}</span>
      <span v-if="refCount" class="pp-stat"><b>{{ refCount }}</b> 张参考图</span>
      <span class="pp-stat"><b>{{ chars }}</b> 字</span>
      <span class="pp-gap" />
      <button type="button" class="pp-btn" :disabled="isEmpty" @click="copy">复制</button>
      <button type="button" class="pp-btn is-toggle" :aria-expanded="editing" @click="editing = !editing">
        {{ editing ? '完成' : '编辑提示词' }}
      </button>
    </header>

    <p v-if="isEmpty" class="pp-empty">{{ placeholder }}</p>

    <textarea
      v-else-if="editing"
      class="pp-input"
      :value="modelValue"
      rows="14"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    />

    <div v-else class="pp-view">
      <p v-if="body" class="pp-body">{{ body }}</p>
      <p v-else-if="plainBody" class="pp-body">{{ plainBody }}</p>
      <dl v-if="rest.length" class="pp-segments">
        <template v-for="seg in rest" :key="seg.name">
          <dt>{{ seg.name }}</dt>
          <dd>{{ seg.text }}</dd>
        </template>
      </dl>
    </div>

    <p v-if="editing" class="pp-hint">
      提示词中的 &lt;imageN&gt; 编号与下方参考图槽位严格对应；提交前会自动校验一致性。
    </p>
  </div>
</template>

<style scoped>
.pp {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.pp-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--text-tertiary);
}
.pp-task {
  max-width: 16em;
  padding: 1px var(--sp-2);
  font-size: 11px;
  line-height: 18px;
  color: var(--brand-hover);
  background: var(--brand-soft);
  border: 1px solid var(--brand-line);
  border-radius: var(--radius-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pp-stat b {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--text-secondary);
}
.pp-gap {
  flex: 1;
  min-width: var(--sp-2);
}
.pp-btn {
  padding: 1px var(--sp-2);
  font-family: inherit;
  font-size: 11px;
  line-height: 18px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.pp-btn:hover:not(:disabled) {
  color: var(--brand-hover);
  border-color: var(--brand-line);
}
.pp-btn:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.pp-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.pp-btn.is-toggle {
  color: var(--text-primary);
}

/* 正文按「可读文档」排版：稍大字号、更松行高，扫描 400 字不费劲 */
.pp-body {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.85;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}
.pp-segments {
  display: grid;
  grid-template-columns: 5.5em minmax(0, 1fr);
  gap: 6px var(--sp-3);
  margin: var(--sp-3) 0 0;
  padding-top: var(--sp-3);
  border-top: 1px dashed var(--border-subtle);
  font-size: 12px;
}
.pp-segments dt {
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 20px;
  letter-spacing: 0.02em;
  color: var(--text-tertiary);
}
.pp-segments dd {
  margin: 0;
  line-height: 20px;
  color: var(--text-secondary);
  word-break: break-word;
}
.pp-empty {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-tertiary);
}
.pp-input {
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  font-size: 12.5px;
  line-height: 1.7;
  font-family: var(--font-mono);
  color: var(--text-primary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  resize: vertical;
}
.pp-input:focus-visible {
  outline: none;
  border-color: var(--brand-hover);
  box-shadow: var(--focus-ring);
}
.pp-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

@media (max-width: 768px) {
  .pp {
    padding: var(--sp-3);
  }
  .pp-segments {
    grid-template-columns: 1fr;
    gap: 2px;
  }
  .pp-segments dd {
    margin-bottom: var(--sp-2);
  }
}
</style>
