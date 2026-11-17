<script setup lang="ts">
/**
 * 「生成」标签页：按当前阶段只展示可行的生成动作
 * （未确认分镜图先出图，确认后再出视频），并保留镜头音频面板入口。
 */
import { NButton, NSelect, NTooltip } from 'naive-ui'
import ShotAudioPanel from '@/components/ShotAudioPanel.vue'

defineProps<{
  projectId: string
  shotId: string
  episode: number
  readinessAll: boolean
  keyframeConfirmed: boolean
  clipActionLabel: string
  clipActionIsRegenerate: boolean
  generating: boolean
  directRegenLoading: boolean
  tierOptions: Array<{ label: string; value: string }>
}>()

const tier = defineModel<string>('tier', { required: true })

const emit = defineEmits<{ 'generate-keyframe': []; 'clip-action': [] }>()
</script>

<template>
  <div class="generate-tab">
    <div v-if="!readinessAll" class="gate-hint">
      公共资产尚未齐全（角色定妆图 / 场景基准图 / 道具锚定图），请先到资产库补齐后再生成。
    </div>

    <div class="gen-line">
      <span class="line-label">当前阶段</span>
      <span class="line-value">
        {{ keyframeConfirmed ? '分镜图已确认，可生成视频' : '待确认分镜图，先出关键帧' }}
      </span>
    </div>

    <div class="gen-line">
      <span class="line-label">视频档位</span>
      <n-select
        v-model:value="tier"
        :options="tierOptions"
        size="small"
        class="tier-select"
      />
      <span class="line-hint">极速/标准 5s、质量 10s 分别对应一套 4 步工作流</span>
    </div>

    <div class="gen-actions">
      <n-tooltip v-if="!keyframeConfirmed">
        <template #trigger>
          <n-button
            type="primary"
            :disabled="!readinessAll"
            :loading="generating"
            @click="emit('generate-keyframe')"
          >
            生成分镜图
          </n-button>
        </template>
        {{ readinessAll ? '提交分镜图（关键帧）生成任务' : '公共资产尚未齐全，暂不能生成分镜图' }}
      </n-tooltip>
      <n-tooltip v-else>
        <template #trigger>
          <n-button
            type="primary"
            :disabled="!readinessAll"
            :loading="generating || directRegenLoading"
            @click="emit('clip-action')"
          >
            {{ clipActionLabel }}
          </n-button>
        </template>
        {{
          readinessAll
            ? clipActionIsRegenerate
              ? '重新生成视频：使用当前提示词、参考图与档位重新生成，完成后替换原视频'
              : '提交视频生成任务'
            : '公共资产尚未齐全，暂不能生成视频'
        }}
      </n-tooltip>
      <span class="action-hint">
        快捷键 <kbd>Enter</kbd> 执行当前动作，<kbd>R</kbd> 重新生成
      </span>
    </div>

    <section class="audio-block">
      <h4 class="block-title">镜头音频</h4>
      <ShotAudioPanel :project-id="projectId" :shot-id="shotId" :episode="episode" />
    </section>
  </div>
</template>

<style scoped>
.generate-tab {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.gate-hint {
  padding: var(--sp-2) var(--sp-3);
  font-size: 12px;
  line-height: 1.6;
  color: var(--state-warning);
  background: var(--warning-soft);
  border: 1px solid var(--warning-line);
  border-radius: var(--radius);
}
.gen-line {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.line-label {
  font-size: 12px;
  color: var(--text-tertiary);
}
.line-value {
  font-size: 13px;
  color: var(--text-primary);
}
.line-hint {
  font-size: 12px;
  color: var(--text-tertiary);
}
.tier-select {
  width: 210px;
}
.gen-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
  padding: var(--sp-3) 0;
  border-top: 1px solid var(--border-subtle);
  border-bottom: 1px solid var(--border-subtle);
}
.action-hint {
  font-size: 12px;
  color: var(--text-tertiary);
}
kbd {
  padding: 0 5px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 17px;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
.audio-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.block-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
</style>
