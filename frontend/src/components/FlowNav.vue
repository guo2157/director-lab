<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useScriptFlowStore } from '@/stores/scriptFlow'

const props = defineProps<{ projectId: string }>()
const route = useRoute()
const router = useRouter()
const flow = useScriptFlowStore()

/**
 * 剧本阶段子流程导航（Agent 一体化：评估+解析合并，集数由 Agent 决定）
 * 保存 → 解析，禁止跨级跳转
 */
const steps = [
  { key: 'script', label: '剧本' },
  { key: 'parse', label: '解析' },
]

const activeKey = computed(() => route.name as string)

const stepDone = computed<Record<string, boolean>>(() => ({
  script: !!flow.scriptVersionId,
  parse: !!flow.candidateVersionId || flow.parseConfirmed,
}))

/** 单一出口：前置步骤完成才可进入下一步 */
function reachable(key: string): boolean {
  if (key === 'script') return true
  if (key === 'parse') return stepDone.value.script
  return false
}

function goStep(key: string) {
  if (!reachable(key)) return
  void router.push(`/project/${props.projectId}/${key}`)
}
</script>

<template>
  <nav class="flow-nav">
    <template v-for="(s, i) in steps" :key="s.key">
      <span v-if="i > 0" class="flow-sep">/</span>
      <span
        class="flow-step"
        :class="{ active: activeKey === s.key, done: stepDone[s.key], disabled: !reachable(s.key) }"
        @click="goStep(s.key)"
      >
        <span v-if="stepDone[s.key]" class="step-check">✓</span>
        {{ s.label }}
      </span>
    </template>
  </nav>
</template>

<style scoped>
.flow-nav {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-secondary);
  flex-wrap: wrap;
}
.flow-sep {
  color: var(--text-tertiary);
}
.flow-step {
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 6px;
  transition: background 0.15s, color 0.15s;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.flow-step:hover:not(.disabled) {
  background: var(--bg-elevated);
  color: var(--text-primary);
}
.flow-step.active {
  color: var(--brand-primary);
  font-weight: 600;
}
.flow-step.done {
  color: var(--text-primary);
}
.flow-step.disabled {
  color: var(--text-tertiary);
  cursor: not-allowed;
}
.step-check {
  color: var(--state-success);
  font-weight: 700;
}
</style>
