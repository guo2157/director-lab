<script setup lang="ts">
/**
 * 分镜图卡片：缩略图为主体，状态角标压在图上。
 *
 * 信息分层（按 DESIGN.md「内容优先」）：
 *   图 → 镜号 + 标题 → 目标画面摘要（2 行）→ 元信息一行 → 主操作
 * 结构化提示词段落（任务/参考图职责/必须保持/禁止…）不铺在卡上，
 * 只把【目标画面】作为摘要，其余压成 chip，全文在详情弹窗里看。
 */
import { computed } from 'vue'
import { NDropdown } from 'naive-ui'
import StatusTag from '@/components/ui/StatusTag.vue'
import type { Shot } from '@/types/script'
import { shotStatus, shotThumbUrl } from './types'
import { promptDigest } from './promptSummary'

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
const digest = computed(() => promptDigest(props.prompt))
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
  items.push({ label: '查看大图与提示词', key: 'open' })
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

      <span class="sb-badge" :class="{ 'is-quiet': status.kind === 'succeeded' }">
        <StatusTag :status="status.kind" :label="status.label" />
      </span>

      <span v-if="locked" class="sb-lock" title="已锁定，不会被重绘覆盖">已锁定</span>
    </div>

    <div class="sb-body">
      <div class="sb-head">
        <span class="sb-seq">{{ String(seq).padStart(2, '0') }}</span>
        <h3 class="sb-title" :title="title">{{ title }}</h3>
      </div>

      <p v-if="failedMessage" class="sb-error" :title="failedMessage">失败原因：{{ failedMessage }}</p>

      <!-- 摘要区独立于卡片点击：查看全文不应在卡内展开，统一进详情 -->
      <div class="sb-prompt" @click.stop>
        <p v-if="digest.body" class="sb-summary" :title="digest.body">{{ digest.body }}</p>
        <p v-else class="sb-summary is-empty">
          {{ prompt ? '提示词为纯结构化要求，点开查看全文' : '尚未生成分镜图提示词' }}
        </p>

        <div class="sb-meta">
          <span v-if="digest.task" class="sb-chip" :title="digest.task">{{ digest.task }}</span>
          <span v-if="digest.refCount" class="sb-chip">参考图 {{ digest.refCount }}</span>
          <span class="sb-chars">{{ digest.chars }} 字</span>
          <button type="button" class="sb-link" @click="emit('open')">查看全文</button>
        </div>
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
  transition: opacity var(--transition-fast);
}
/* 已完成是常态：26 张卡都挂绿标等于噪声，只在悬停/聚焦时显形 */
.sb-badge.is-quiet {
  opacity: 0.55;
}
.sb-card:hover .sb-badge.is-quiet,
.sb-card:focus-within .sb-badge.is-quiet {
  opacity: 1;
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
  letter-spacing: 0.04em;
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

/* 摘要：正文用界面字体扫描，不用等宽；等宽只留给字数这类数字 */
.sb-prompt {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  min-width: 0;
}
.sb-summary {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.sb-summary.is-empty {
  color: var(--text-tertiary);
}
.sb-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  flex-wrap: wrap;
  font-size: 11px;
  line-height: 18px;
  color: var(--text-tertiary);
}
.sb-chip {
  max-width: 12em;
  padding: 0 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
  background: var(--neutral-soft);
  border: 1px solid var(--neutral-line);
  border-radius: var(--radius-sm);
}
.sb-chars {
  font-family: var(--font-mono);
}
.sb-meta .sb-chip,
.sb-meta .sb-chars {
  opacity: 0.7;
  transition: opacity var(--transition-fast);
}
.sb-card:hover .sb-meta .sb-chip,
.sb-card:hover .sb-meta .sb-chars {
  opacity: 1;
}
.sb-link {
  margin-left: auto;
  padding: 0;
  font-family: inherit;
  font-size: 11px;
  line-height: 18px;
  color: var(--text-tertiary);
  background: none;
  border: 0;
  border-bottom: 1px solid transparent;
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.sb-link:hover {
  color: var(--brand-hover);
  border-bottom-color: var(--brand-line);
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
