<script setup lang="ts">
/**
 * 会话消息流：气泡 + 资产缩略图 + 联网搜索执行卡片 + 任务卡片 + 思考气泡。
 *
 * 纯展示组件：所有真实执行数据来自消息 payload 与 SSE 快照，组件自身只维护
 * 「缩略图加载失败」这一处纯 UI 状态（破图即隐藏，不回写数据）。
 */
import { ref } from 'vue'
import { NIcon, NImage } from 'naive-ui'
import { PlayCircleOutline } from '@vicons/ionicons5'
import TaskCard from '@/components/TaskCard.vue'
import SearchRunCard from './SearchRunCard.vue'
import type { AgentMessage } from '@/api/agent'
import type { TaskCardData, TaskEvent } from '@/api/tasks'
import {
  assetRefsOf,
  isImageAsset,
  searchRecordOf,
  SEARCH_PHASE_TEXT,
  taskCardOf,
} from './helpers'

const props = defineProps<{
  messages: AgentMessage[]
  thinking: boolean
  thinkingStage: string
  /** task_id → SSE 合并后的快照（无则回退初始卡片） */
  snapshots: Record<string, Partial<TaskCardData> & { lastEvent?: TaskEvent }>
}>()

/** 缩略图加载失败的资产（历史消息中资产可能已失效或被清理，失败即隐藏避免破图） */
const failedAssetIds = ref<Set<string>>(new Set())

function onThumbError(assetId: string): void {
  const next = new Set(failedAssetIds.value)
  next.add(assetId)
  failedAssetIds.value = next
}

function visibleAssetRefs(m: AgentMessage) {
  return assetRefsOf(m).filter((a) => !failedAssetIds.value.has(a.asset_id))
}

function roleLabel(role: string): string {
  if (role === 'user') return '我'
  if (role === 'assistant') return '导演台助手'
  return role
}

/** 搜索任务实时快照（仅 running 时展示阶段与进度，终态回落到最终记录） */
function searchLive(m: AgentMessage): { phase: string; progress: number } | null {
  const tid = (searchRecordOf(m) as unknown as { task_id?: string } | null)?.task_id
  if (!tid) return null
  const snap = props.snapshots[tid] as { status?: string; progress?: number; phase?: string } | undefined
  if (!snap || snap.status !== 'running') return null
  const progress = Math.max(0, Math.min(100, snap.progress ?? 0))
  return { phase: SEARCH_PHASE_TEXT[snap.phase ?? ''] ?? '执行中', progress }
}
</script>

<template>
  <div class="message-list">
    <div v-for="m in messages" :key="m.id" class="message-row" :class="m.role">
      <div class="message-bubble">
        <div class="message-role">{{ roleLabel(m.role) }}</div>
        <div class="message-content">{{ m.content }}</div>

        <div v-if="m.role === 'assistant' && visibleAssetRefs(m).length" class="asset-thumbs">
          <template v-for="a in visibleAssetRefs(m)" :key="a.asset_id">
            <!-- 图片类：气泡内用低成本缩略图直出，点击进入预览层查看原图
                 （可缩放/旋转/下载），避免新开标签页直接加载数 MB PNG -->
            <div
              v-if="isImageAsset(a.asset_type)"
              class="asset-thumb"
              :title="`${a.name}（${a.asset_type}）`"
            >
              <NImage
                :src="`/api/assets/${a.asset_id}/file?thumb=true`"
                :preview-src="`/api/assets/${a.asset_id}/file`"
                :img-props="{ alt: a.name, loading: 'lazy' }"
                width="100%"
                height="100%"
                object-fit="cover"
                @error="onThumbError(a.asset_id)"
              />
              <span class="asset-thumb-label">{{ a.asset_type }}</span>
            </div>
            <a
              v-else
              class="asset-thumb-link"
              :href="`/api/assets/${a.asset_id}/file`"
              target="_blank"
              rel="noopener"
              :title="`${a.name}（${a.asset_type}）`"
            >
              <n-icon :component="PlayCircleOutline" :size="13" /> {{ a.asset_type }}
            </a>
          </template>
        </div>

        <!-- 联网搜索执行卡片：展示真实搜索执行记录（来自后端 payload.search，非 LLM 生成） -->
        <SearchRunCard
          v-if="m.role === 'assistant' && searchRecordOf(m)"
          :record="searchRecordOf(m)!"
          :live="searchLive(m)"
        />

        <!-- 任务卡片：消费 payload.task_card，SSE 事件驱动单卡片动态更新 -->
        <TaskCard
          v-if="m.role === 'assistant' && taskCardOf(m)"
          :card="taskCardOf(m)!"
          :snapshot="snapshots[taskCardOf(m)!.task_id] ?? null"
        />
      </div>
    </div>

    <!-- 思考过程气泡：等待响应期间分阶段展示，响应到达后由最终回复替换 -->
    <div v-if="thinking" class="message-row assistant">
      <div class="message-bubble thinking-bubble">
        <div class="message-role">导演台助手</div>
        <div class="message-content thinking-content">
          {{ thinkingStage }}<span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.message-list {
  padding: var(--sp-5) var(--sp-6);
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  flex: 1;
  min-height: min-content;
}

.message-row {
  display: flex;
  animation: msg-fade-in 0.22s var(--ease-out);
}

@keyframes msg-fade-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.message-row.user {
  justify-content: flex-end;
}

.message-row.user .message-bubble {
  background: var(--brand-soft);
  border-color: var(--brand-line);
  border-radius: var(--radius-xl) var(--radius-sm) var(--radius-xl) var(--radius-xl);
}

.message-bubble {
  max-width: 76%;
  padding: var(--sp-3) var(--sp-4);
  border-radius: var(--radius-sm) var(--radius-xl) var(--radius-xl) var(--radius-xl);
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  transition: border-color var(--transition-fast);
}

.message-bubble:hover {
  border-color: var(--border-strong);
}

.thinking-bubble {
  border-style: dashed;
}

.thinking-content {
  display: flex;
  align-items: center;
  color: var(--text-secondary);
}

.thinking-dots {
  display: inline-flex;
  gap: var(--sp-1);
  margin-left: var(--sp-1);
}

.thinking-dots i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: currentColor;
  animation: thinking-blink 1.2s infinite ease-in-out;
}

.thinking-dots i:nth-child(2) {
  animation-delay: 0.2s;
}

.thinking-dots i:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes thinking-blink {
  0%,
  80%,
  100% {
    opacity: 0.25;
  }
  40% {
    opacity: 1;
  }
}

.message-role {
  font-size: 11px;
  line-height: 19px;
  color: var(--text-tertiary);
  margin-bottom: var(--sp-1);
}

.message-content {
  font-size: 14px;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
}

.message-content :deep(a) {
  color: var(--brand-primary);
  text-decoration: none;
  border-bottom: 1px solid var(--brand-line);
  transition: border-color var(--transition-fast);
}

.message-content :deep(a:hover) {
  border-bottom-color: var(--brand-primary);
}

.message-content :deep(code) {
  background: var(--bg-sunken);
  padding: 1px var(--sp-2);
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-family: var(--font-mono);
}

.message-content :deep(pre) {
  background: var(--bg-sunken);
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  overflow-x: auto;
  margin: var(--sp-2) 0;
}

.message-content :deep(pre code) {
  background: none;
  padding: 0;
  border: none;
}

/* 气泡内资产缩略图（agent asset_refs） */
.asset-thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}

.asset-thumb {
  position: relative;
  display: block;
  width: 96px;
  height: 96px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--border-subtle);
  background: var(--media-backdrop);
  cursor: zoom-in;
}

.asset-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.asset-thumb-label {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  font-size: 10px;
  padding: 2px var(--sp-1);
  background: var(--scrim);
  color: var(--text-primary);
  text-align: center;
}

.asset-thumb-link {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 96px;
  height: 96px;
  border-radius: var(--radius-lg);
  border: 1px dashed var(--border-subtle);
  font-size: 12px;
  color: var(--text-secondary);
  text-decoration: none;
}

.asset-thumb-link:hover {
  border-color: var(--brand-primary);
  color: var(--brand-hover);
}

/* 联网搜索执行卡片（agent web_search payload.search）：见 SearchRunCard */

@media (max-width: 768px) {
  .message-list {
    padding: var(--sp-4);
    gap: var(--sp-4);
  }
  .message-bubble {
    max-width: 88%;
  }
}
</style>
