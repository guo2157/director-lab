<script setup lang="ts">
/**
 * 卡片底部操作条：主操作「重新生成 / 替换上传」常驻，不进下拉。
 *
 * 2026-11 流程简化后删除：
 * - 确认 / 取消确认（逐镜人工确认闸口已取消，阶段推进改为产物就绪判定）
 * - 锁定 / 解锁（锁定功能整体移除）
 * - ⋯「查看详情与提示词」下拉（详情改为直接点卡片进入，操作条不再留第二个入口）
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    hasKeyframe: boolean
    /** 在途任务文案，空表示无在途任务 */
    inflightText?: string
    busy?: boolean
    /** 主操作文案：重新生成 / 生成分镜图 / 生成中 42% */
    primaryLabel: string
  }>(),
  {
    inflightText: '',
    busy: false,
  },
)

const emit = defineEmits<{
  generate: []
  replace: []
}>()

const generateDisabled = computed(() => !!props.inflightText || props.busy)
const generateTitle = computed(() => {
  if (props.inflightText) return props.inflightText
  return props.hasKeyframe ? '用当前提示词重新生成本镜分镜图' : '按当前提示词生成本镜分镜图'
})

const replaceDisabled = computed(() => !props.hasKeyframe || !!props.inflightText || props.busy)
const replaceTitle = computed(() => {
  if (!props.hasKeyframe) return '尚无分镜图，生成后才能替换'
  if (props.inflightText) return '生成中，稍后再替换'
  return '用本地上传的图替换该镜分镜图'
})
</script>

<template>
  <footer class="sb-actions" @click.stop>
    <button
      type="button"
      class="sb-act is-primary"
      :disabled="generateDisabled"
      :title="generateTitle"
      @click="emit('generate')"
    >
      {{ primaryLabel }}
    </button>
    <button
      type="button"
      class="sb-act is-ghost"
      :disabled="replaceDisabled"
      :title="replaceTitle"
      @click="emit('replace')"
    >
      替换上传
    </button>
  </footer>
</template>

<style scoped>
.sb-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border-subtle);
}
.sb-act {
  font-family: inherit;
  font-size: 12px;
  line-height: 22px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  white-space: nowrap;
  transition: color var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
}
.sb-act:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.sb-act:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.sb-act.is-primary {
  flex: 1;
  min-width: 0;
  padding: 0 var(--sp-2);
  font-weight: 500;
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border: 1px solid var(--brand-primary);
  overflow: hidden;
  text-overflow: ellipsis;
}
.sb-act.is-primary:hover:not(:disabled) {
  background: var(--brand-hover);
  border-color: var(--brand-hover);
}
.sb-act.is-ghost {
  flex: none;
  padding: 0 var(--sp-2);
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-subtle);
}
.sb-act.is-ghost:hover:not(:disabled) {
  color: var(--brand-hover);
  border-color: var(--brand-line);
  background: var(--brand-soft);
}
</style>
