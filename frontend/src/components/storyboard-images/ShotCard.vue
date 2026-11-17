<script setup lang="ts">
/**
 * 分镜图卡片：缩略图为主体（固定 16:9，网格不抖动），状态角标压在图上，
 * 底部只留最高频的一个主操作（重新生成 / 生成），其余收进「更多」。
 */
import { computed } from 'vue'
import { NDropdown } from 'naive-ui'
import StatusTag from '@/components/ui/StatusTag.vue'
import PromptEditor from '@/components/ui/PromptEditor.vue'
import type { Shot } from '@/types/script'
import { shotStatus, shotThumbUrl } from './types'

const props = withDefaults(
  defineProps<{
    shot: Shot
    /** 全片镜号 */
    seq: number
    selected?: boolean
    selectable?: boolean
    /** 在途任务文案（排队中 / 生成中 42%），空表示无在途任务 */
    inflightText?: string
    failed?: boolean
    failedMessage?: string
    /** 该卡有写操作在进行（锁定 / 替换） */
    busy?: boolean
    /** 公共资产审核通过才可生成 */
    canGenerate?: boolean
    prompt: string
  }>(),
  {
    selected: false,
    selectable: true,
    inflightText: '',
    failed: false,
    failedMessage: '',
    busy: false,
    canGenerate: true,
    prompt: '',
  },
)

const emit = defineEmits<{
  select: [boolean]
  open: []
  generate: []
  confirm: []
  unconfirm: []
  lock: []
  replace: []
  'update:prompt': [string]
}>()

const thumbUrl = computed(() => shotThumbUrl(props.shot))
const locked = computed(() => Boolean(props.shot.current_assets?.keyframe?.locked))
const hasKeyframe = computed(() => Boolean(props.shot.current_assets?.keyframe))
const status = computed(() => shotStatus(props.shot, { inflightText: props.inflightText, failed: props.failed }))
const title = computed(() => props.shot.title || `镜头 ${props.seq}`)
const primaryLabel = computed(() => {
  if (props.inflightText) return props.inflightText
  return hasKeyframe.value ? '重新生成' : '生成分镜图'
})

/** 低频操作全部收进下拉：确认 / 取消确认 / 锁定 / 替换上传 / 详情 */
const menuOptions = computed(() => {
  const items: Array<Record<string, unknown>> = []
  if (hasKeyframe.value && !props.shot.keyframe_confirmed) {
    items.push({ label: '确认分镜图', key: 'confirm' })
  }
  if (props.shot.keyframe_confirmed) {
    items.push({ label: '取消确认', key: 'unconfirm' })
  }
  if (hasKeyframe.value) {
    items.push({ label: locked.value ? '解锁' : '锁定', key: 'lock' })
  }
  if (hasKeyframe.value && !locked.value) {
    items.push({ label: '替换上传', key: 'replace' })
  }
  items.push({ type: 'divider', key: 'd1' })
  items.push({ label: '查看大图与参考图', key: 'open' })
  return items
})

function onPick(e: Event) {
  emit('select', (e.target as HTMLInputElement).checked)
}

function onMenu(key: string | number) {
  const k = String(key)
  if (k === 'confirm') emit('confirm')
  else if (k === 'unconfirm') emit('unconfirm')
  else if (k === 'lock') emit('lock')
  else if (k === 'replace') emit('replace')
  else if (k === 'open') emit('open')
}
</script>

<template>
  <article
    class="sb-card"
    :class="{
      'is-selected': selected,
      'is-inflight': !!inflightText,
      'is-failed': failed,
      'is-blank': !hasKeyframe,
    }"
    @click="emit('open')"
  >
    <div class="sb-thumb">
      <img v-if="thumbUrl" :src="thumbUrl" :alt="`镜 ${seq} 分镜图`" loading="lazy" />
      <div v-else class="sb-thumb-empty">
        <span class="sb-thumb-hint">{{ inflightText || '尚未生成分镜图' }}</span>
      </div>

      <label v-if="selectable" class="sb-pick" @click.stop>
        <input type="checkbox" :checked="selected" :aria-label="`勾选镜 ${seq}`" @change="onPick" />
      </label>

      <span class="sb-badge">
        <StatusTag :status="status.kind" :label="status.label" />
      </span>

      <span v-if="locked" class="sb-lock" title="已锁定，不会被重绘覆盖">已锁定</span>
    </div>

    <div class="sb-body">
      <div class="sb-head">
        <span class="sb-seq">镜 {{ seq }}</span>
        <h3 class="sb-title" :title="title">{{ title }}</h3>
      </div>

      <p v-if="failedMessage" class="sb-error" :title="failedMessage">失败原因：{{ failedMessage }}</p>

      <!-- 提示词区域独立于卡片点击：展开编辑 / 复制不应打开详情弹窗 -->
      <div class="sb-prompt" @click.stop>
        <PromptEditor
          :model-value="prompt"
          label="提示词"
          :rows="5"
          :collapsed-lines="2"
          placeholder="尚未生成分镜图提示词"
          @update:model-value="(v: string) => emit('update:prompt', v)"
        />
      </div>
    </div>

    <footer class="sb-actions" @click.stop>
      <button
        type="button"
        class="sb-act is-primary"
        :disabled="!canGenerate || locked || !!inflightText || busy"
        :title="canGenerate ? primaryLabel : '需先通过公共资产人工审核'"
        @click="emit('generate')"
      >
        {{ primaryLabel }}
      </button>
      <n-dropdown trigger="click" :options="menuOptions" @select="onMenu">
        <button type="button" class="sb-act is-icon" aria-label="更多操作" title="更多操作">⋯</button>
      </n-dropdown>
    </footer>
  </article>
</template>

<style scoped>
.sb-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  overflow: hidden;
  cursor: pointer;
  transition: border-color var(--transition-fast);
}
.sb-card:hover {
  border-color: var(--border-strong);
}
.sb-card.is-selected {
  border-color: var(--brand-line);
}
.sb-card.is-inflight {
  border-color: var(--brand-line);
}
.sb-card.is-failed {
  border-color: var(--danger-line);
}

/* 图为卡片主体：比例跟随项目画幅（--sb-thumb-ratio），图片未到时高度不变 */
.sb-thumb {
  position: relative;
  width: 100%;
  aspect-ratio: var(--sb-thumb-ratio, 16 / 9);
  background: var(--bg-sunken);
  border-bottom: 1px solid var(--border-subtle);
}
.sb-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: opacity var(--transition-fast);
}
.sb-card:hover .sb-thumb img {
  opacity: 0.92;
}
.sb-thumb-empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--sp-2);
  text-align: center;
}
.sb-thumb-hint {
  font-size: 12px;
  color: var(--text-tertiary);
}

.sb-pick {
  position: absolute;
  top: var(--sp-2);
  left: var(--sp-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  cursor: pointer;
  z-index: 2;
}
.sb-pick input {
  width: 13px;
  height: 13px;
  margin: 0;
  accent-color: var(--brand-primary);
  cursor: pointer;
}
.sb-badge {
  position: absolute;
  top: var(--sp-2);
  right: var(--sp-2);
  z-index: 2;
  display: inline-flex;
  padding: 1px;
  background: var(--scrim);
  border-radius: var(--radius-sm);
}
.sb-lock {
  position: absolute;
  left: var(--sp-2);
  bottom: var(--sp-2);
  z-index: 2;
  padding: 0 var(--sp-2);
  font-size: 11px;
  line-height: 18px;
  color: var(--text-secondary);
  background: var(--neutral-soft);
  border: 1px solid var(--neutral-line);
  border-radius: var(--radius-sm);
}

.sb-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  flex: 1;
  min-width: 0;
  padding: var(--sp-3);
}
.sb-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  min-width: 0;
}
.sb-seq {
  flex: none;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
}
.sb-title {
  margin: 0;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-error {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--danger);
  background: var(--danger-soft);
  border-radius: var(--radius-sm);
  padding: 2px var(--sp-2);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/* 提示词块嵌在卡片里：去掉重复的边框与底色，避免卡片内再套一层卡片 */
.sb-body :deep(.prompt-editor) {
  padding: 0;
  gap: var(--sp-1);
  background: transparent;
  border: 0;
  border-radius: 0;
}

.sb-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-sunken);
}
.sb-act {
  padding: 2px var(--sp-3);
  font-family: inherit;
  font-size: 12px;
  line-height: 20px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
}
.sb-act:hover:not(:disabled) {
  color: var(--brand-hover);
  border-color: var(--brand-line);
}
.sb-act:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.sb-act.is-primary {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border-color: var(--brand-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-act.is-primary:hover:not(:disabled) {
  color: var(--text-on-brand);
  background: var(--brand-hover);
  border-color: var(--brand-hover);
}
.sb-act.is-icon {
  flex: none;
  min-width: 28px;
  font-size: 14px;
  line-height: 20px;
}
</style>
