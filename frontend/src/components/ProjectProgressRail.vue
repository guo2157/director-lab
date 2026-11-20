<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NIcon } from 'naive-ui'
import { AlertCircle, Checkmark, LockClosedOutline } from '@vicons/ionicons5'
import { useProjectFlow } from '@/composables/useProjectFlow'
import { STAGE_LABELS } from '@/constants/stages'

const props = defineProps<{ projectId: string }>()
const route = useRoute()
const router = useRouter()

/**
 * 制作流程竖列：6 个独立步骤（剧本 / 解析 / 资产库 / 分镜图 / 分镜看板 / 合成与导出）。
 *
 * 状态口径唯一来源是 useProjectFlow —— 与页内共用同一份 Query 缓存，
 * 刷新页面、直接访问 /parse 都能得到正确状态，不依赖内存里的 store。
 * 「解析」已从剧本阶段的子步骤独立成节点（2026-11-20），因此不再有
 * 「按路由回落到所属父步骤」的逻辑，/parse 高亮 02 解析。
 */
const routeName = computed(() => String(route.name ?? ''))
const { steps: stepMap, activeKey, project } = useProjectFlow(
  computed(() => props.projectId),
  routeName,
)

/** 有序步骤数组：显式排序，避免对象键序被误改 */
const ORDER = ['script', 'parse', 'assets', 'storyboard-images', 'storyboard', 'compose'] as const
const steps = computed(() => ORDER.map((k) => stepMap.value[k]))

const stageText = computed(
  () => STAGE_LABELS[project.value?.current_stage ?? ''] ?? project.value?.current_stage ?? '',
)

function goStep(key: string) {
  void router.push(`/project/${props.projectId}/${key}`)
}

/**
 * 锁定态按钮的可访问名称：真 disabled 元素在很多浏览器里不派发 title 提示，
 * 因此把原因同时写进 aria-label，读屏用户也能知道「为什么点不动」。
 */
function ariaLabel(key: string, label: string, status: string, hint: string) {
  const i = ORDER.indexOf(key as (typeof ORDER)[number]) + 1
  const no = String(i).padStart(2, '0')
  if (status === 'locked') return `第 ${no} 步 ${label}（已锁定：${hint}）`
  const stateText =
    status === 'done' ? '已完成' : status === 'running' ? '进行中' : status === 'failed' ? '有失败待处理' : '未开始'
  return `第 ${no} 步 ${label}（${stateText}）`
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
        v-for="(s, i) in steps"
        :key="s.key"
        class="rail-step"
        :class="[`is-${s.status}`, { 'is-active': activeKey === s.key }]"
      >
        <!-- 锁定步骤用真 disabled（不是只改样式），并保留 aria-disabled 供辅助技术识别 -->
        <button
          type="button"
          class="step-hit"
          :disabled="s.status === 'locked'"
          :aria-disabled="s.status === 'locked' || undefined"
          :aria-current="activeKey === s.key ? 'page' : undefined"
          :title="s.status === 'locked' ? s.hint : undefined"
          :aria-label="ariaLabel(s.key, s.label, s.status, s.hint)"
          @click="goStep(s.key)"
        >
          <span class="step-index">{{ String(i + 1).padStart(2, '0') }}</span>
          <span class="step-label">{{ s.label }}</span>
          <!-- 行尾状态标记：形状区分（勾 / 脉冲点 / 警示三角），不只靠颜色 -->
          <n-icon
            v-if="s.status === 'done'"
            class="step-mark is-done"
            :component="Checkmark"
            :size="13"
            aria-hidden="true"
          />
          <span v-else-if="s.status === 'running'" class="step-mark is-running" aria-hidden="true" />
          <n-icon
            v-else-if="s.status === 'failed'"
            class="step-mark is-failed"
            :component="AlertCircle"
            :size="13"
            aria-hidden="true"
          />
          <!-- 锁定：锁图标让「点不动」在形状上可读，不必靠悬停才发现原因 -->
          <n-icon
            v-else-if="s.status === 'locked'"
            class="step-mark is-locked"
            :component="LockClosedOutline"
            :size="12"
            aria-hidden="true"
          />
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

.step-hit:hover:not(:disabled) {
  color: var(--text-primary);
  background: var(--overlay-hover);
}

.step-hit:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
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

/* 行尾状态标记：靠右对齐，编号列保持 01–06 统一 */
.step-mark {
  margin-left: auto;
  flex-shrink: 0;
}
.step-mark.is-done {
  color: var(--brand-primary);
}
.step-mark.is-failed {
  color: var(--danger-text);
}
/* 锁定：锁图标压到最弱档，只表达「不可达」，不参与视觉竞争 */
.step-mark.is-locked {
  color: var(--text-disabled);
}
/* 进行中：脉冲点，与解析页集数格 is-running 同一语汇（形状 + 动效双编码） */
.step-mark.is-running {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--brand-primary);
  animation: step-pulse 1.6s ease-in-out infinite;
}
@keyframes step-pulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 var(--brand-line);
  }
  50% {
    box-shadow: 0 0 0 4px var(--brand-soft);
  }
}
@media (prefers-reduced-motion: reduce) {
  .step-mark.is-running {
    animation: none;
  }
}

.rail-step.is-done .step-index {
  color: var(--brand-primary);
  border-color: var(--brand-line);
  background: var(--brand-soft);
}

.rail-step.is-done .step-label {
  color: var(--text-secondary);
}

/* 进行中：编号点亮到品牌底，与「已完成」靠行尾标记区分（点 vs 勾） */
.rail-step.is-running .step-index {
  color: var(--brand-hover);
  border-color: var(--brand-line);
  background: var(--brand-soft);
}
.rail-step.is-running .step-label {
  color: var(--text-primary);
}

/* 失败：编号保持中性，只让行尾警示与文字承担告警，避免整列被红色压住 */
.rail-step.is-failed .step-label {
  color: var(--danger-text);
}
.rail-step.is-failed .step-index {
  color: var(--danger-text);
  border-color: var(--danger-line);
  background: var(--danger-soft);
}

/* 未到达（既非当前、也未完成）：明确降权。
   原实现里「未开始」和「可点击但没去」是同一套灰，用户看不出哪些是真不能去。
   做法：序号压到最弱档 + 文字降到 tertiary，与 is-done / is-active 形成多档。 */
.rail-step.is-todo .step-index {
  color: var(--text-disabled);
  background: transparent;
  border-color: var(--border-subtle);
}
.rail-step.is-todo .step-label {
  color: var(--text-tertiary);
}
/* 未到达步骤的 hover 仍要有反馈（它们可以点，只是还没做） */
.rail-step.is-todo .step-hit:hover .step-label {
  color: var(--text-primary);
}

/* 锁定：真禁用。文字与编号压到 disabled 档，光标改为 not-allowed —— 三重提示不可点 */
.rail-step.is-locked .step-hit {
  cursor: not-allowed;
}
.rail-step.is-locked .step-index {
  color: var(--text-disabled);
  background: transparent;
  border-color: var(--border-subtle);
  border-style: dashed;
}
.rail-step.is-locked .step-label {
  color: var(--text-disabled);
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

/* 当前步骤若同时是失败态：编号仍反色（我在这一页），但文字保留告警色 */
.rail-step.is-active.is-failed .step-label {
  color: var(--danger-text);
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
  /* 窄屏：隐藏「制作流程」标题，把宽度让给 6 个步骤 */
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
  /* 窄屏隐藏 01–06 序号：阶段语义由 label 承载，序号只占宽度。
     但状态标记必须保留 —— 它是唯一还能表达「完成/进行中/失败」的信号。 */
  .step-index {
    display: none;
  }
  .step-mark {
    margin-left: 3px;
  }
}

@media (max-width: 480px) {
  /* 390px 下 6 步横排会截断第 6 步「合成与导出」——横向滚动把流程末端藏起来，
     而「还剩几步」正是这条栏要回答的问题。改为换行成两行，6 步全部可见。 */
  .progress-rail {
    overflow-x: visible;
    padding: var(--sp-2) var(--sp-2);
  }
  .rail-steps {
    flex-wrap: wrap;
    justify-content: flex-start;
    gap: 2px 6px;
  }
  .rail-step {
    flex: 0 0 auto;
  }
  .step-hit {
    padding: 5px 6px;
    min-height: 30px;
  }
  .step-label {
    font-size: 11.5px;
  }
  .step-mark {
    margin-left: 4px;
  }
}
</style>
