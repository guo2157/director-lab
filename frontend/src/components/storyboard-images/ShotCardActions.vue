<script setup lang="ts">
/**
 * 卡片底部操作条：主操作「重新生成 / 替换上传」常驻，不进下拉。
 * ⋯ 里只放低频项（确认 / 取消确认 / 锁定 / 解锁 / 详情）。
 */
import { computed } from 'vue'
import { NDropdown } from 'naive-ui'

const props = withDefaults(
  defineProps<{
    hasKeyframe: boolean
    locked?: boolean
    confirmed?: boolean
    /** 在途任务文案，空表示无在途任务 */
    inflightText?: string
    canGenerate?: boolean
    busy?: boolean
    /** 主操作文案：重新生成 / 生成分镜图 / 生成中 42% */
    primaryLabel: string
  }>(),
  {
    locked: false,
    confirmed: false,
    inflightText: '',
    canGenerate: true,
    busy: false,
  },
)

const emit = defineEmits<{
  generate: []
  replace: []
  confirm: []
  unconfirm: []
  lock: []
  open: []
}>()

const generateDisabled = computed(
  () => !props.canGenerate || props.locked || !!props.inflightText || props.busy,
)
const generateTitle = computed(() => {
  if (!props.canGenerate) return '需先通过公共资产人工审核'
  if (props.locked) return '该镜已锁定，先解锁再生成'
  if (props.inflightText) return props.inflightText
  return props.hasKeyframe ? '用当前提示词重新生成本镜分镜图' : '按当前提示词生成本镜分镜图'
})

const replaceDisabled = computed(
  () => !props.hasKeyframe || props.locked || !!props.inflightText || props.busy,
)
const replaceTitle = computed(() => {
  if (!props.hasKeyframe) return '尚无分镜图，生成后才能替换'
  if (props.locked) return '该镜已锁定，先解锁再替换'
  if (props.inflightText) return '生成中，稍后再替换'
  return '用本地上传的图替换该镜分镜图'
})

const menuOptions = computed(() => {
  const items: Array<Record<string, unknown>> = []
  if (props.hasKeyframe && !props.confirmed) items.push({ label: '确认分镜图', key: 'confirm' })
  if (props.confirmed) items.push({ label: '取消确认', key: 'unconfirm' })
  if (props.hasKeyframe) items.push({ label: props.locked ? '解锁' : '锁定', key: 'lock' })
  items.push({ type: 'divider', key: 'd1' })
  items.push({ label: '查看大图与提示词', key: 'open' })
  return items
})

function onMenu(key: string | number) {
  const k = String(key)
  if (k === 'confirm') emit('confirm')
  else if (k === 'unconfirm') emit('unconfirm')
  else if (k === 'lock') emit('lock')
  else if (k === 'open') emit('open')
}
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
    <n-dropdown trigger="click" :options="menuOptions" @select="onMenu">
      <button type="button" class="sb-act is-more" aria-label="更多操作" title="更多操作">⋯</button>
    </n-dropdown>
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
.sb-act.is-more {
  flex: none;
  min-width: 26px;
  padding: 0 4px;
  font-size: 14px;
  color: var(--text-tertiary);
  background: transparent;
  border: 1px solid transparent;
}
.sb-act.is-more:hover {
  color: var(--text-primary);
  border-color: var(--border-subtle);
}
</style>
