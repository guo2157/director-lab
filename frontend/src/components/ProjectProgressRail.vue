<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { NIcon } from 'naive-ui'
import { Checkmark } from '@vicons/ionicons5'
import { getProject } from '@/api/projects'
import { STAGE_LABELS, STAGE_ORDER } from '@/constants/stages'

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

/** 后端阶段枚举 → 中文阶段名（唯一来源：@/constants/stages） */
const stageLabels = STAGE_LABELS

const { data: project } = useQuery({
  queryKey: computed(() => ['project', props.projectId]),
  queryFn: () => getProject(props.projectId),
})

// 后端阶段 → 前端流程步骤索引（按顺序取第一个匹配）
const stageOrder = STAGE_ORDER as readonly string[]

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

// 当前步骤：先按路由名直接匹配，未命中时回落到「覆盖该阶段的父步骤」
// （如 /parse 属于剧本阶段的子页面，应高亮 01 剧本，而不是整列没有高亮）
const activeKey = computed(() => {
  const name = route.name as string
  const direct = stages.find((s) => s.key === name)
  if (direct) return direct.key
  const owner = stages.find((s) => (s.stages as readonly string[]).includes(name))
  return owner?.key ?? name
})

function goStep(key: string) {
  void router.push(`/project/${props.projectId}/${key}`)
}
</script>

<template>
  <aside class="progress-rail" aria-label="制作流程">
    <div class="rail-head">
      <span class="rail-title">制作流程</span>
      <span v-if="stageText" class="rail-stage">
        <span class="rail-stage-key">进度</span>{{ stageText }}
      </span>
    </div>

    <ol class="rail-steps">
      <li
        v-for="(s, i) in stages"
        :key="s.key"
        class="rail-step"
        :class="{ 'is-active': activeKey === s.key, 'is-done': stepDone[i] }"
      >
        <button type="button" class="step-hit" @click="goStep(s.key)">
          <span class="step-index">{{ String(i + 1).padStart(2, '0') }}</span>
          <span class="step-label">{{ s.label }}</span>
          <!-- 完成态用行尾勾选标记表示，编号列始终保持 01–05 统一风格 -->
          <n-icon v-if="stepDone[i]" class="step-done-mark" :component="Checkmark" :size="13" />
        </button>
      </li>
    </ol>
  </aside>
</template>

<style scoped>
.progress-rail {
  width: var(--app-rail-width, 216px);
  flex-shrink: 0;
  padding: var(--sp-6) var(--sp-3);
  background: var(--bg-sunken);
  border-right: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
}

.rail-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 var(--sp-2);
}

.rail-title {
  font-family: var(--font-display);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  /* 原 --text-tertiary 在 --bg-sunken 上只有 6.65:1，但 12px 小字仍偏弱；
     提到 --text-secondary 让它作为「栏目标题」站得住 */
  color: var(--text-secondary);
}

/* 「当前阶段 xxx」：前缀用小号弱色标注，避免这行字被误读为可点的步骤项 */
.rail-stage {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-size: 12.5px;
  color: var(--brand-primary);
  letter-spacing: 0.01em;
}
.rail-stage-key {
  font-size: 10.5px;
  color: var(--text-tertiary);
  letter-spacing: 0.02em;
}

.rail-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.rail-step {
  position: relative;
}

/* 竖向刻度线：连接各步骤，已完成段用品牌色 */
.rail-step:not(:last-child)::after {
  content: '';
  position: absolute;
  left: calc(var(--sp-2) + 12px);
  top: 32px;
  bottom: -4px;
  width: 1px;
  background: var(--border-subtle);
}

.rail-step.is-done:not(:last-child)::after {
  background: var(--brand-deep);
}

.step-hit {
  appearance: none;
  position: relative;
  width: 100%;
  background: none;
  border: 0;
  font-family: inherit;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px var(--sp-2);
  min-height: 32px;
  border-radius: var(--radius-md);
  cursor: pointer;
  text-align: left;
  color: var(--text-secondary);
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast);
}

.step-hit:hover {
  color: var(--text-primary);
  background: var(--overlay-hover);
}

/* 编号：圆角方块，是这一列的结构骨架 */
.step-index {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1;
  color: var(--text-tertiary);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast),
    border-color var(--transition-fast);
}

.step-label {
  font-size: 13.5px;
  letter-spacing: 0.01em;
  white-space: nowrap;
}

/* 完成态勾选标记：靠右对齐，编号列保持 01–05 统一 */
.step-done-mark {
  margin-left: auto;
  flex-shrink: 0;
  color: var(--brand-primary);
}

.rail-step.is-done .step-index {
  color: var(--brand-primary);
  border-color: var(--brand-line);
  background: var(--brand-soft);
}

.rail-step.is-done .step-label {
  color: var(--text-secondary);
}

/* 未到达的步骤（既非当前、也未完成）：明确降权。
   原实现里「未开始」和「可点击但没去」是同一套灰，用户看不出哪些是真不能去。
   做法：序号压到最弱档 + 文字降到 tertiary，与 is-done / is-active 形成三档。 */
.rail-step:not(.is-done):not(.is-active) .step-index {
  color: var(--text-disabled);
  background: transparent;
  border-color: var(--border-subtle);
}
.rail-step:not(.is-done):not(.is-active) .step-label {
  color: var(--text-tertiary);
}
/* 未到达步骤的 hover 仍要有反馈（它们可以点，只是还没做） */
.rail-step:not(.is-done):not(.is-active) .step-hit:hover .step-label {
  color: var(--text-primary);
}

/* 当前步骤：soft 品牌底 + 左侧竖条 + 编号反色 —— 三重编码，深色底上单靠明度差不可读 */
.rail-step.is-active .step-hit {
  color: var(--text-primary);
  background: var(--brand-soft);
  box-shadow: inset 0 0 0 1px var(--brand-line);
}
.rail-step.is-active .step-hit::before {
  content: '';
  position: absolute;
  left: 0;
  top: 7px;
  bottom: 7px;
  width: 2px;
  border-radius: var(--radius-pill);
  background: var(--brand-gradient);
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
  /* 窄屏：隐藏「制作流程」标题，把宽度让给 5 个步骤，让全部阶段一屏可见 */
  .rail-head {
    display: none;
  }
  .rail-stage,
  .rail-step::after {
    display: none;
  }
  .rail-steps {
    flex-direction: row;
    gap: 2px;
    width: 100%;
    justify-content: space-between;
  }
  .step-hit {
    padding: 6px 6px;
    min-height: 34px;
    gap: 0;
  }
  .step-label {
    font-size: 12px;
  }
  /* 窄屏隐藏 01–05 序号：阶段语义由 label 承载，序号只占宽度 */
  .step-index {
    display: none;
  }
}
</style>
