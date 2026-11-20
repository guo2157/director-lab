<script setup lang="ts">
/**
 * 资产卡片：一张图 + 名字 + 一句描述。
 * 有图时图是主体（悬停才浮出操作），没有图时卡片本身就是「去生成」的入口。
 * 角色卡多一条音色条：音色是角色资产的第二个必配项，不能藏在抽屉里。
 */
import { computed } from 'vue'
import { NIcon } from 'naive-ui'
import { ChevronForwardOutline, MicOutline } from '@vicons/ionicons5'
import StatusTag from '@/components/ui/StatusTag.vue'
import { assetImgUrl } from './assetMedia'
import { ASSET_KIND_LABELS, ASSET_UNIT_LABELS, type AssetCardItem } from './types'

const props = withDefaults(
  defineProps<{
    item: AssetCardItem
    selected?: boolean
    selectable?: boolean
    /** 在途任务的文案（生成中 42% / 排队中），空表示没有在途任务 */
    inflightText?: string
    failed?: boolean
    /** 角色当前音色名，未配置为空 */
    voiceName?: string
  }>(),
  { selected: false, selectable: false, inflightText: '', failed: false, voiceName: '' },
)

const emit = defineEmits<{
  open: []
  generate: []
  replace: []
  regenerate: []
  voice: []
  'update:selected': [boolean]
}>()

const thumbUrl = computed(() => assetImgUrl(props.item.asset))
const kindLabel = computed(() => ASSET_KIND_LABELS[props.item.kind])
const unitLabel = computed(() => ASSET_UNIT_LABELS[props.item.kind])

type TagKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'stale'
const status = computed<{ kind: TagKind; label: string }>(() => {
  if (props.inflightText) return { kind: 'generating', label: props.inflightText }
  if (props.failed) return { kind: 'failed', label: '生成失败' }
  if (props.item.done || props.item.asset) return { kind: 'succeeded', label: '已生成' }
  return { kind: 'pending', label: '待生成' }
})

function onPick(e: Event) {
  emit('update:selected', (e.target as HTMLInputElement).checked)
}
function onDragStart(e: DragEvent) {
  const asset = props.item.asset
  if (!asset || !e.dataTransfer) return
  e.dataTransfer.setData('application/x-asset-id', asset.id)
  e.dataTransfer.setData('text/plain', asset.id)
  e.dataTransfer.effectAllowed = 'copy'
}
</script>

<template>
  <article
    class="a-card"
    :class="{
      'is-selected': selected,
      'is-blank': !item.asset,
      'is-inflight': !!inflightText,
      'is-failed': failed,
    }"
    :draggable="!!item.asset"
    @dragstart="onDragStart"
    @click="emit('open')"
  >
    <div class="a-thumb" :class="`k-${item.kind}`">
      <img v-if="thumbUrl" :src="thumbUrl" :alt="item.name" loading="lazy" />
      <div v-else class="a-thumb-empty">
        <span class="a-thumb-kind">{{ kindLabel }}</span>
        <span class="a-thumb-hint">{{ item.done ? '图片待更新' : `尚未生成${unitLabel}` }}</span>
      </div>
      <label v-if="selectable" class="a-pick" @click.stop>
        <input type="checkbox" :checked="selected" @change="onPick" />
      </label>
    </div>

    <div class="a-body">
      <div class="a-head">
        <h3 class="a-name" :title="item.name">{{ item.name }}</h3>
        <StatusTag :status="status.kind" :label="status.label" />
      </div>
      <p class="a-desc" :title="item.desc">{{ item.desc || '暂无描述' }}</p>
    </div>

    <button
      v-if="item.kind === 'character'"
      type="button"
      class="a-voice"
      :title="voiceName ? `当前音色：${voiceName}` : '尚未配置音色'"
      @click.stop="emit('voice')"
    >
      <span class="a-voice-icon" aria-hidden="true"><n-icon :component="MicOutline" :size="14" /></span>
      <span class="a-voice-label">音色</span>
      <span class="a-voice-value">{{ voiceName || '未设置' }}</span>
      <span class="a-voice-arrow" aria-hidden="true"><n-icon :component="ChevronForwardOutline" :size="13" /></span>
    </button>

    <footer class="a-actions" @click.stop>
      <button
        v-if="!item.asset"
        type="button"
        class="a-act is-primary"
        :disabled="!!inflightText"
        @click="emit('generate')"
      >
        生成{{ unitLabel }}
      </button>
      <template v-else>
        <button type="button" class="a-act" @click="emit('replace')">替换</button>
        <button type="button" class="a-act" @click="emit('regenerate')">重生成</button>
      </template>
    </footer>
  </article>
</template>

<style scoped>
.a-card {
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
.a-card:hover {
  border-color: var(--border-strong);
}
.a-card.is-selected {
  border-color: var(--brand-line);
  background: var(--overlay-selected);
}
.a-card.is-inflight {
  border-color: var(--brand-line);
}
.a-card.is-failed {
  border-color: var(--danger-line);
}

/* 图为卡片主体：固定比例，网格对齐不跳动。
   min-height: 0 必须显式声明 —— flex item 默认 min-height:auto，
   会让内容（img 的固有尺寸）把容器撑破 aspect-ratio。
   img 必须绝对定位 —— 否则 height:100% 相对 aspect-ratio 推导出的高度
   形成循环依赖，浏览器回退到 img 固有比例，实测把 3:4 的 368px 撑到 482px。 */
.a-thumb {
  position: relative;
  width: 100%;
  aspect-ratio: 4 / 5;
  min-height: 0;
  overflow: hidden;
  background: var(--bg-sunken);
  border-bottom: 1px solid var(--border-subtle);
}
.a-thumb.k-character {
  aspect-ratio: 4 / 5;
}
.a-thumb img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center top;
  display: block;
}
.a-thumb-empty {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-1);
  color: var(--text-tertiary);
}
.a-thumb-kind {
  font-size: 13px;
  letter-spacing: 0.08em;
  color: var(--text-secondary);
}
.a-thumb-hint {
  font-size: 12px;
}

.a-pick {
  position: absolute;
  top: var(--sp-2);
  left: var(--sp-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.a-pick input {
  width: 13px;
  height: 13px;
  margin: 0;
  accent-color: var(--brand-primary);
  cursor: pointer;
}

.a-body {
  padding: var(--sp-3) var(--sp-3) var(--sp-2);
  flex: 1;
  min-width: 0;
}
.a-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}
.a-name {
  margin: 0;
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.a-desc {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.a-voice {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-2) var(--sp-3);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.4;
  text-align: left;
  color: var(--text-secondary);
  background: transparent;
  border: 0;
  border-top: 1px solid var(--border-subtle);
  cursor: pointer;
  transition: color var(--transition-fast), background var(--transition-fast);
}
.a-voice:hover {
  color: var(--brand-hover);
  background: var(--overlay-hover);
}
.a-voice-icon {
  flex: none;
}
.a-voice-label {
  flex: none;
  font-weight: 600;
}
.a-voice-value {
  flex: 1;
  min-width: 0;
  text-align: right;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-tertiary);
}
.a-voice-arrow {
  flex: none;
  opacity: 0.6;
}

.a-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-sunken);
}
.a-act {
  padding: 2px var(--sp-2);
  font-family: inherit;
  font-size: 12px;
  line-height: 20px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.a-act:hover:not(:disabled) {
  color: var(--brand-hover);
  border-color: var(--brand-line);
}
.a-act:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.a-act.is-primary {
  margin-right: auto;
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border-color: var(--brand-primary);
  font-weight: 500;
}
.a-act.is-primary:hover:not(:disabled) {
  color: var(--text-on-brand);
  background: var(--brand-hover);
  border-color: var(--brand-hover);
}
</style>
