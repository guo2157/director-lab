<script setup lang="ts">
/**
 * 风格基线只读摘要（完整编辑在剧本页）。
 * 枚举值的中文展示名由后端 styleOptions 下发，前端不硬编码枚举。
 */
import { NButton } from 'naive-ui'
import type { StyleConfig, StyleOptions } from '@/types/project'
import EmptyState from '@/components/ui/EmptyState.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'

const props = defineProps<{
  config: StyleConfig | null
  label: string
  options: StyleOptions | null
  loading: boolean
}>()

const emit = defineEmits<{ go: [page: string] }>()

/** 把后端下发的枚举值映射为中文展示名 */
function labelOf(
  list: Array<{ value: string; label: string }> | undefined,
  value: string | undefined,
): string {
  if (!value) return '未设置'
  return list?.find((i) => i.value === value)?.label ?? value
}
</script>

<template>
  <div class="style-baseline">
    <div class="style-head">
      <span class="style-caption">当前项目的风格约束</span>
      <NButton size="tiny" quaternary @click="emit('go', 'script')">去调整 →</NButton>
    </div>

    <SkeletonList v-if="loading && !props.config" variant="row" :rows="4" />

    <EmptyState
      v-else-if="!props.config"
      compact
      title="尚未读取风格配置"
      description="在剧本页设置风格基线后，这里会显示摘要"
      action-text="去剧本页"
      @action="emit('go', 'script')"
    />

    <div v-else class="style-lines">
      <div class="style-line">
        <span class="style-k">视觉风格</span>
        <span class="style-v">{{ label || props.config.visual_style }}</span>
      </div>
      <div class="style-line">
        <span class="style-k">剧本类型</span>
        <span class="style-v">
          {{ props.config.genre_tags.length ? props.config.genre_tags.join('、') : '未设置' }}
        </span>
      </div>
      <div class="style-line">
        <span class="style-k">剧情节奏</span>
        <span class="style-v">{{ labelOf(props.options?.pacing, props.config.pacing) }}</span>
      </div>
      <div class="style-line">
        <span class="style-k">主情绪</span>
        <span class="style-v">{{ props.config.emotional_tone[0] ?? '未设置' }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.style-baseline {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.style-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}

.style-caption {
  font-size: 12px;
  color: var(--text-tertiary);
}

.style-lines {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.style-line {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: 12px;
  line-height: 18px;
}

.style-k {
  flex: 0 0 56px;
  color: var(--text-secondary);
}

.style-v {
  flex: 1 1 auto;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
