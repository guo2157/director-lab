<script setup lang="ts">
/**
 * 镜头详情左栏：成片优先的预览区。
 * 有视频先放视频，其次分镜图；黑场用 --media-backdrop 保持画幅（letterbox），
 * 不做拉伸裁切。左右切换镜头由详情弹窗统一处理，这里只负责发出意图。
 */
import { NTag } from 'naive-ui'
import StatusTag from '@/components/ui/StatusTag.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { Shot } from '@/types/script'

type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'

defineProps<{
  shot: Shot
  /** 全片镜号（当前集内 1..N） */
  seq: number
  statusKind: StatusKind
  statusLabel: string
  kindLabel: string
  canPrev: boolean
  canNext: boolean
}>()

const emit = defineEmits<{ navigate: [delta: 1 | -1] }>()
</script>

<template>
  <div class="preview-pane">
    <header class="pane-head">
      <div class="head-text">
        <span class="pane-eyebrow">镜头 {{ seq }} · {{ kindLabel }}</span>
        <h3 class="pane-title">{{ shot.title || `镜头 ${seq}` }}</h3>
      </div>
      <StatusTag :status="statusKind" :label="statusLabel" pulse />
    </header>

    <!-- 媒体区：保持画幅，不足部分补黑场 -->
    <div class="media-frame">
      <video
        v-if="shot.current_assets?.clip?.url"
        :src="shot.current_assets.clip.url"
        controls
        preload="metadata"
        class="media-el"
      />
      <img
        v-else-if="shot.current_assets?.keyframe?.url || shot.current_assets?.keyframe?.thumb_url"
        :src="shot.current_assets.keyframe!.url || shot.current_assets.keyframe!.thumb_url!"
        :alt="`镜头 ${seq} 分镜图`"
        class="media-el is-image"
      />
      <EmptyState
        v-else
        compact
        class="media-empty"
        title="该镜头尚未生成视频"
        :description="
          shot.keyframe_confirmed
            ? '分镜图已确认，可在「生成」标签页提交视频生成。'
            : '需先到分镜图环节确认关键帧，之后才能生成视频。'
        "
      >
        <template #icon>▷</template>
      </EmptyState>

      <button
        type="button"
        class="nav-btn is-prev"
        :disabled="!canPrev"
        aria-label="上一个镜头"
        title="上一个镜头（←）"
        @click="emit('navigate', -1)"
      >‹</button>
      <button
        type="button"
        class="nav-btn is-next"
        :disabled="!canNext"
        aria-label="下一个镜头"
        title="下一个镜头（→）"
        @click="emit('navigate', 1)"
      >›</button>
    </div>

    <div class="pane-meta">
      <n-tag size="small" :bordered="false">{{ kindLabel }}</n-tag>
      <span class="meta-item">第 {{ shot.episode }} 集 · 全片第 {{ seq }} 镜</span>
      <span class="meta-item">{{ shot.duration }} 秒</span>
      <span v-if="shot.current_assets?.clip?.locked" class="meta-item">已锁定</span>
    </div>

    <p v-if="shot.description" class="pane-desc">{{ shot.description }}</p>
  </div>
</template>

<style scoped>
.preview-pane {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
}
.pane-head {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  justify-content: space-between;
}
.head-text {
  min-width: 0;
}
.pane-eyebrow {
  display: block;
  font-size: 12px;
  line-height: 19px;
  color: var(--text-tertiary);
}
.pane-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.media-frame {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  aspect-ratio: 9 / 16;
  max-height: 62vh;
  background: var(--media-backdrop);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  overflow: hidden;
}
.media-el {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: var(--media-backdrop);
  display: block;
}
.media-el.is-image {
  object-fit: contain;
}
.media-empty {
  width: 100%;
  height: 100%;
  border: none;
  background: transparent;
}
.nav-btn {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  line-height: 1;
  color: var(--text-secondary);
  background: var(--overlay-active);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.nav-btn.is-prev {
  left: var(--sp-2);
}
.nav-btn.is-next {
  right: var(--sp-2);
}
.nav-btn:hover:not(:disabled) {
  color: var(--brand-hover);
  border-color: var(--brand-line);
}
.nav-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}
.pane-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.meta-item {
  font-size: 12px;
  color: var(--text-secondary);
}
.pane-desc {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
