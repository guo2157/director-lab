<script setup lang="ts">
/**
 * 剧本页三步流程条：剧本编辑 → 内容校验 → 版本已存。纯展示。
 *
 * 视觉要点（对齐 pixmax 语汇）：
 * 1. 步骤之间必须有连接线 —— 否则三段文字只是一排并列标签，读不出「流程」。
 * 2. 三档状态靠「形状 + 颜色」双编码，不能只靠颜色深浅：
 *      已完成 = 实心浅底 + 对勾
 *      当前   = 品牌胶囊 + 发光描边 + 序号
 *      未到达 = 透明底 + 虚线描边 + 序号
 * 3. 所有档位共用同一套 padding/border 宽度，切换时不发生位移。
 *
 * 状态去重（2026-11-21）：本组件曾额外渲染「已锁稿」徽标，加上页头的
 * 「版本已保存」、右侧的禁用按钮「已锁稿」，同一件事在一屏里说了 4 遍。
 * 现在职责单一：**本组件只回答「流程走到第几步」**，保存/锁稿状态统一由
 * 页头状态点 + 右侧按钮文案表达。
 * 因此不再需要 savedVersion / locked 两个 props。
 */
import { NIcon } from 'naive-ui'
import { Checkmark } from '@vicons/ionicons5'

defineProps<{
  steps: ReadonlyArray<{ key: string; label: string }>
  /** 当前阶段下标；之前的视为已完成 */
  index: number
}>()
</script>

<template>
  <div class="stage-bar">
    <template v-for="(s, i) in steps" :key="s.key">
      <div
        class="stage-item"
        :class="{ 'is-active': index === i, 'is-done': index > i, 'is-todo': index < i }"
        :aria-current="index === i ? 'step' : undefined"
      >
        <span class="stage-index">
          <n-icon v-if="index > i" :component="Checkmark" :size="12" />
          <template v-else>{{ i + 1 }}</template>
        </span>
        <span class="stage-label">{{ s.label }}</span>
      </div>
      <!-- 连接线：只在「已经走到」的段位点亮，未到达的段位保持中性，让进度方向一眼可读 -->
      <span
        v-if="i < steps.length - 1"
        class="stage-link"
        :class="{ 'is-done': index > i }"
        aria-hidden="true"
      />
    </template>
  </div>
</template>

<style scoped>
.stage-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  flex-shrink: 0;
  flex-wrap: wrap;
  row-gap: 6px;
}

/* 三档共用底座：padding / border 宽度一致，激活态只换颜色不换盒子尺寸 */
.stage-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px 3px 4px;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 12px;
  line-height: 1;
  color: var(--text-tertiary);
  transition: background-color 0.16s ease, border-color 0.16s ease, color 0.16s ease;
}

/* 当前阶段：品牌胶囊 + 极淡光晕，一眼看出「我在这一步」 */
.stage-item.is-active {
  background: var(--brand-soft);
  border-color: var(--brand-line);
  box-shadow: 0 0 0 3px var(--brand-glow);
  color: var(--brand-primary);
  font-weight: 600;
}
.stage-item.is-active .stage-index {
  background: var(--brand-gradient);
  border-color: transparent;
  color: var(--text-on-brand);
  font-weight: 700;
}

/* 已完成：实心浅绿底 + 对勾，形状上与「序号」明确区分 */
.stage-item.is-done {
  color: var(--text-secondary);
}
.stage-item.is-done .stage-index {
  background: var(--success-soft);
  border-color: var(--success-line);
  color: var(--state-success);
}

/* 未到达：虚线描边 + 最弱文字，与已完成/当前形成第三档 */
.stage-item.is-todo .stage-index {
  border-style: dashed;
  border-color: var(--border-strong);
  color: var(--text-disabled);
}
.stage-item.is-todo .stage-label {
  color: var(--text-tertiary);
}

.stage-index {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  border: 1px solid currentColor;
  flex-shrink: 0;
  transition: background-color 0.16s ease, border-color 0.16s ease, color 0.16s ease;
}

/* .stage-extra 已删除（2026-11-21）：它只服务于「已锁稿」徽标，
   而该徽标与页头状态、右侧按钮文案三处重复，已移除。
   保留空规则会让后来者以为还有额外状态位。 */

/* 连接线：走完的段位用品牌渐变，未走的用中性描边色 */
.stage-link {
  width: 18px;
  height: 1px;
  flex-shrink: 0;
  background: var(--border-strong);
}
.stage-link.is-done {
  background: var(--brand-gradient-line);
}

@media (max-width: 768px) {
  /* 窄屏下连接线缩短，优先保住四个步骤标签的可读性 */
  .stage-link {
    width: 10px;
  }
  .stage-item {
    padding: 3px 8px 3px 3px;
  }
}
</style>
