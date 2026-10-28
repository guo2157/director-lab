<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import { getParseStatus } from '@/api/projects'

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

/** 后端真实的集级解析状态：刷新页面后仍可据此判断解析是否完成 */
const { data: parseStatusData } = useQuery({
  queryKey: computed(() => ['parse-status', props.projectId]),
  queryFn: () => getParseStatus(props.projectId),
  enabled: computed(() => !!props.projectId),
})

/**
 * 全部集均已解析成功（依据后端记录做严格判定）：
 * 后端只返回已有解析记录的集，未解析过的集不会出现在 records 中，
 * 因此不能只看『记录是否都成功』，必须比对总集数。
 * 另：同一集可能按不同资产类型存有多条记录，需按集号去重后再统计。
 */
const parseAllSucceeded = computed(() => {
  const records = parseStatusData.value?.records ?? []
  if (!records.length) return false
  const statusByEpisode = new Map<number, string>()
  for (const r of records) {
    const prev = statusByEpisode.get(r.episode_no)
    // 同一集有多条记录时，任一条非成功即视为该集未完成
    if (prev === undefined) statusByEpisode.set(r.episode_no, r.status)
    else if (prev === 'succeeded' && r.status !== 'succeeded') statusByEpisode.set(r.episode_no, r.status)
  }
  let ok = 0
  for (const st of statusByEpisode.values()) if (st === 'succeeded') ok += 1
  // 总集数：优先取后端锁定的值，其次用剧本评估给出的集数兜底
  const total = parseStatusData.value?.total_episodes ?? flow.totalEpisodes ?? 0
  // 已知总集数：必须每一集都成功；未知总集数：以现有记录全部成功为准
  if (total > 0) return ok >= total && statusByEpisode.size >= total
  return ok === statusByEpisode.size
})

const stepDone = computed<Record<string, boolean>>(() => ({
  script: !!flow.scriptVersionId,
  parse: !!flow.candidateVersionId || flow.parseConfirmed || parseAllSucceeded.value,
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
  font-size: 15px;
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
  font-size: 16px;
  line-height: 1;
  margin-right: 2px;
}
</style>
