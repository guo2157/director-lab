<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { getProject } from '@/api/projects'

const props = defineProps<{ projectId: string }>()
const route = useRoute()
const router = useRouter()

// 流程阶段定义：key 对应路由名，stage 对应后端 current_stage 枚举
const stages = [
  { key: 'script', label: '剧本', icon: '📝', stages: ['estimate', 'select_range', 'parse'] },
  // 资产库：公共资产（角色定妆图/场景基准图/道具锚定图）生成
  { key: 'assets', label: '资产库', icon: '🖼️', stages: ['characters', 'scenes'] },
  // 分镜图：公共资产人工审核 + 逐镜分镜图生成与确认
  { key: 'storyboard-images', label: '分镜图', icon: '✅', stages: ['assets_review', 'keyframes'] },
  { key: 'storyboard', label: '分镜看板', icon: '🎬', stages: ['clips', 'audio'] },
  { key: 'compose', label: '合成与导出', icon: '📦', stages: ['compose', 'export', 'done'] },
]

const { data: project } = useQuery({
  queryKey: computed(() => ['project', props.projectId]),
  queryFn: () => getProject(props.projectId),
})

// 后端阶段 → 前端流程步骤索引（按顺序取第一个匹配）
const stageOrder = [
  'estimate', 'select_range', 'parse',
  'characters', 'scenes', 'assets_review', 'keyframes', 'clips', 'audio',
  'compose', 'export', 'done',
]
const currentStage = computed(() => project.value?.current_stage ?? '')

// 每个流程步骤是否已完成：当前阶段索引大于该步骤覆盖的最大阶段索引
const stepDone = computed(() => stages.map((s) => {
  if (currentStage.value === 'done') return true
  const idx = stageOrder.indexOf(currentStage.value)
  if (idx < 0) return false
  const maxIdx = Math.max(...s.stages.map((st) => stageOrder.indexOf(st)))
  return idx > maxIdx
}))

const activeKey = computed(() => route.name as string)

function goStep(key: string) {
  void router.push(`/project/${props.projectId}/${key}`)
}
</script>

<template>
  <aside class="progress-rail">
    <div class="rail-title">制作流程</div>
    <div class="rail-steps">
      <div
        v-for="(s, i) in stages"
        :key="s.key"
        class="rail-step"
        :class="{ active: activeKey === s.key, done: stepDone[i] }"
        @click="goStep(s.key)"
      >
        <div class="step-marker">
          <span v-if="stepDone[i]" class="step-check">✓</span>
          <span v-else class="step-icon">{{ s.icon }}</span>
        </div>
        <span class="step-label">{{ s.label }}</span>
        <div v-if="i < stages.length - 1" class="step-line" :class="{ filled: stepDone[i] }" />
      </div>
    </div>
    <div v-if="currentStage" class="rail-stage">当前阶段：{{ currentStage }}</div>
  </aside>
</template>

<style scoped>
.progress-rail {
  width: 168px;
  flex-shrink: 0;
  padding: 20px 12px;
  background: var(--bg-panel);
  border-right: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.rail-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  letter-spacing: 0.05em;
  padding: 0 8px;
}
.rail-steps {
  display: flex;
  flex-direction: column;
}
.rail-step {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 8px;
  border-radius: 8px;
  cursor: pointer;
  position: relative;
  color: var(--text-secondary);
  transition: background 0.15s, color 0.15s;
}
.rail-step:hover {
  background: var(--bg-elevated);
  color: var(--text-primary);
}
.rail-step.active {
  background: var(--bg-elevated);
  color: var(--brand-primary);
}
.rail-step.done {
  color: var(--text-primary);
}
.step-marker {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: 1.5px solid var(--border-strong);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  flex-shrink: 0;
  background: var(--bg-base);
}
.rail-step.done .step-marker {
  border-color: var(--state-success);
  background: rgba(34, 197, 94, 0.12);
}
.step-check {
  color: var(--state-success);
  font-size: 13px;
  font-weight: 700;
}
.step-icon {
  font-size: 13px;
}
.step-label {
  font-size: 13px;
}
.step-line {
  position: absolute;
  left: 20px;
  top: 44px;
  width: 2px;
  height: 14px;
  background: var(--border-subtle);
}
.step-line.filled {
  background: var(--state-success);
}
.rail-stage {
  margin-top: auto;
  font-size: 11px;
  color: var(--text-tertiary);
  padding: 0 8px;
  word-break: break-all;
}
@media (max-width: 768px) {
  .progress-rail {
    width: 100%;
    flex-direction: row;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-right: none;
    border-bottom: 1px solid var(--border-subtle);
    overflow-x: auto;
  }
  .rail-title,
  .rail-stage,
  .step-line {
    display: none;
  }
  .rail-steps {
    flex-direction: row;
    gap: 4px;
  }
  .rail-step {
    flex-shrink: 0;
    padding: 6px 10px;
  }
}
</style>
