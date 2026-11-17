<script setup lang="ts">
/**
 * 分镜图卡片：缩略图为主体，状态角标压在图上。
 *
 * 信息分层（按 DESIGN.md「内容优先」）：
 *   图 → 镜号 + 标题 → 一行画面摘要 → 常驻操作条（ShotCardActions）
 * 提示词全文、任务类型、参考图数量都在详情弹窗里看——卡片只留一行摘要，
 * 不在每张卡上堆条件标签。
 */
import { computed } from 'vue'
import StatusTag from '@/components/ui/StatusTag.vue'
import type { Shot } from '@/types/script'
import { shotStatus, shotThumbUrl } from './types'
import { promptDigest } from './promptSummary'
import ShotCardActions from './ShotCardActions.vue'

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

/** 卡片摘要：只取【目标画面】一行；其余结构化段落进详情看 */
const summary = computed(() => {
  const raw = (props.prompt || '').trim()
  if (!raw) return ''
  const digest = promptDigest(raw)
  if (digest.body) return digest.body
  return digest.structured ? '结构化提示词：任务与画面要求见详情' : raw.replace(/\s+/g, ' ')
})

function onPick(e: Event) {
  emit('select', (e.target as HTMLInputElement).checked)
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
    tabindex="0"
    @click="emit('open')"
    @keydown.enter.prevent="emit('open')"
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

      <p v-if="failedMessage" class="sb-error" :title="failedMessage">失败：{{ failedMessage }}</p>

      <p v-if="summary" class="sb-summary" :title="summary">{{ summary }}</p>
      <p v-else class="sb-summary is-empty">尚未生成分镜图提示词</p>
    </div>

    <ShotCardActions
      :has-keyframe="hasKeyframe"
      :locked="locked"
      :confirmed="!!shot.keyframe_confirmed"
      :inflight-text="inflightText"
      :can-generate="canGenerate"
      :busy="busy"
      :primary-label="primaryLabel"
      @generate="emit('generate')"
      @replace="emit('replace')"
      @confirm="emit('confirm')"
      @unconfirm="emit('unconfirm')"
      @lock="emit('lock')"
      @open="emit('open')"
    />
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
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.sb-card:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-card);
}
.sb-card:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}
.sb-card.is-selected,
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
  opacity: 0.94;
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
  line-height: 1.5;
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
  gap: 6px;
  flex: 1;
  min-width: 0;
  padding: var(--sp-3) var(--sp-3) var(--sp-2);
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

/* 摘要只留一行：正文用界面字体扫描，等宽只留给镜号这类数字 */
.sb-summary {
  margin: 0;
  min-height: 20px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-summary.is-empty {
  color: var(--text-tertiary);
}
</style>
