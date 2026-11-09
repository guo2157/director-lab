<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { getProject } from '@/api/projects'

const props = defineProps<{ projectId: string }>()
const route = useRoute()
const router = useRouter()

// 流程阶段定义：key 对应路由名，stages 对应后端 current_stage 枚举
const stages = [
  { key: 'script', label: '剧本', stages: ['estimate', 'select_range', 'parse'] },
  // 资产库：公共资产（角色定妆图/场景基准图/道具锚定图）生成
  { key: 'assets', label: '资产库', stages: ['characters', 'scenes'] },
  // 分镜图：公共资产人工审核 + 逐镜分镜图生成与确认
  { key: 'storyboard-images', label: '分镜图', stages: ['assets_review', 'keyframes'] },
  { key: 'storyboard', label: '分镜看板', stages: ['clips', 'audio'] },
  { key: 'compose', label: '合成与导出', stages: ['compose', 'export', 'done'] },
]

/** 后端阶段枚举 → 中文阶段名 */
const stageLabels: Record<string, string> = {
  estimate: '剧本评估',
  select_range: '选取范围',
  parse: '剧本解析',
  characters: '角色定妆',
  scenes: '场景基准',
  assets_review: '资产审核',
  keyframes: '分镜出图',
  clips: '分镜成片',
  audio: '音频制作',
  compose: '合成',
  export: '导出',
  done: '已完成',
}

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

const stageText = computed(() => stageLabels[currentStage.value] ?? currentStage.value)

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
  <aside class="progress-rail" aria-label="制作流程">
    <div class="rail-head">
      <span class="rail-title">制作流程</span>
      <span v-if="stageText" class="rail-stage">{{ stageText }}</span>
    </div>

    <ol class="rail-steps">
      <li
        v-for="(s, i) in stages"
        :key="s.key"
        class="rail-step"
        :class="{ 'is-active': activeKey === s.key, 'is-done': stepDone[i] }"
      >
        <button type="button" class="step-hit" @click="goStep(s.key)">
          <span class="step-index">{{ stepDone[i] ? '✓' : String(i + 1).padStart(2, '0') }}</span>
          <span class="step-label">{{ s.label }}</span>
        </button>
      </li>
    </ol>
  </aside>
</template>

<style scoped>
.progress-rail {
  width: var(--app-rail-width, 212px);
  flex-shrink: 0;
  padding: var(--sp-5) 0;
  background: var(--bg-sunken);
  border-right: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
}

.rail-head {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 0 var(--sp-5);
}

.rail-title {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--text-primary);
}

.rail-stage {
  font-size: 12px;
  color: var(--brand-primary);
  letter-spacing: 0.01em;
}

.rail-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}

.rail-step {
  position: relative;
}

/* 竖向刻度线：连接各步骤，已完成段用品牌色 */
.rail-step:not(:last-child)::after {
  content: '';
  position: absolute;
  left: calc(var(--sp-5) + 11px);
  top: 30px;
  bottom: -2px;
  width: 1px;
  background: var(--border-subtle);
}

.rail-step.is-done:not(:last-child)::after {
  background: var(--brand-deep);
}

.step-hit {
  appearance: none;
  width: 100%;
  background: none;
  border: 0;
  font-family: inherit;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px var(--sp-5);
  min-height: 30px;
  cursor: pointer;
  text-align: left;
  color: var(--text-secondary);
  transition: color var(--transition-fast), background var(--transition-fast);
}

.step-hit:hover {
  color: var(--text-primary);
  background: rgba(255, 255, 255, 0.025);
}

.step-index {
  flex-shrink: 0;
  width: 23px;
  height: 23px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1;
  color: var(--text-tertiary);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.step-label {
  font-size: 13.5px;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

.rail-step.is-done .step-index {
  color: var(--brand-primary);
  border-color: var(--brand-deep);
  background: rgba(53, 179, 166, 0.08);
}

.rail-step.is-done .step-label {
  color: var(--text-secondary);
}

.rail-step.is-active .step-hit {
  color: var(--text-primary);
  background: var(--bg-panel);
}

.rail-step.is-active .step-index {
  color: var(--text-on-brand);
  background: var(--brand-primary);
  border-color: var(--brand-primary);
  font-weight: 700;
}

.rail-step.is-active .step-label {
  font-weight: 600;
}

@media (max-width: 768px) {
  .progress-rail {
    width: 100%;
    flex-direction: row;
    align-items: center;
    gap: var(--sp-3);
    padding: var(--sp-2) var(--sp-3);
    border-right: none;
    border-bottom: 1px solid var(--border-subtle);
    overflow-x: auto;
  }
  .rail-head {
    padding: 0;
    flex-shrink: 0;
  }
  .rail-stage,
  .rail-step::after {
    display: none;
  }
  .rail-steps {
    flex-direction: row;
    gap: var(--sp-1);
  }
  .step-hit {
    padding: 6px 8px;
    min-height: 28px;
  }
}
</style>
