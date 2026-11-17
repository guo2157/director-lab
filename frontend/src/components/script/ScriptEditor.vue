<script setup lang="ts">
/**
 * 剧本编辑器：版心内的一整块 textarea。
 * 不做假高亮镜像层——那层只为一段从未触发的高亮逻辑服务，反而让光标与文本可能错位。
 */
withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
  }>(),
  { placeholder: '' },
)

const emit = defineEmits<{ 'update:modelValue': [string] }>()
</script>

<template>
  <div class="editor-pane">
    <div class="editor-wrap">
      <div class="editor-column">
        <textarea
          class="editor-input"
          :value="modelValue"
          aria-label="Markdown 剧本正文"
          :placeholder="placeholder"
          spellcheck="false"
          @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
        ></textarea>
      </div>
    </div>
  </div>
</template>

<style scoped>
.editor-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

/* 版心 880px：与阅读区同宽，一行不横穿整屏 */
.editor-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  justify-content: center;
}
.editor-column {
  position: relative;
  width: 100%;
  max-width: 880px;
}
.editor-input {
  position: absolute;
  inset: 0;
  width: 100%;
  padding: var(--sp-4) var(--sp-6);
  font-family: var(--font-mono);
  font-size: 14px;
  line-height: 1.9;
  color: var(--text-primary);
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  caret-color: var(--brand-primary);
}
.editor-input::placeholder {
  color: var(--text-secondary);
}

@media (max-width: 768px) {
  .editor-input {
    padding: var(--sp-3) var(--sp-4);
  }
}
</style>
