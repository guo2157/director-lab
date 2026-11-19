<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import { getParseStatus } from '@/api/projects'
import { NIcon } from 'naive-ui'
import { Checkmark } from '@vicons/ionicons5'

const props = defineProps<{ projectId: string }>()
const route = useRoute()
const router = useRouter()
const flow = useScriptFlowStore()

/**
 * 剧本阶段子流程导航：剧本 → 解析，禁止跨级跳转。
 * 『评估』步骤已移除：剧本保存后由 Agent 一体化完成评估与解析，不再单设评估页入口。
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
  if (key === 'parse') {
    // 解析已完成时允许直接回看；否则有剧本版本即可进入（评估与解析由 Agent 一体化完成）
    return stepDone.value.parse || stepDone.value.script
  }
  return false
}

function goStep(key: string) {
  if (!reachable(key)) return
  void router.push(`/project/${props.projectId}/${key}`)
}
</script>

<template>
  <nav class="flow-nav" aria-label="剧本阶段流程">
    <button
      v-for="s in steps"
      :key="s.key"
      type="button"
      class="flow-step"
      :class="{ active: activeKey === s.key, done: stepDone[s.key], blocked: !reachable(s.key) }"
      :disabled="!reachable(s.key)"
      :aria-current="activeKey === s.key ? 'page' : undefined"
      :title="!reachable(s.key) ? '需先保存剧本版本才能进入解析' : undefined"
      @click="goStep(s.key)"
    >
      <span v-if="stepDone[s.key]" class="step-check" aria-hidden="true">
        <n-icon :component="Checkmark" :size="11" />
      </span>
      {{ s.label }}
    </button>
  </nav>
</template>

<style scoped>
/* 页面级切换：必须是页头最强的导航元素。
   原实现是无边框裸文字 + 「/」分隔，读起来像面包屑，导致真正的换页入口
   比下面的步骤条和视图切换都弱（层级倒挂）。
   深色底上「轨道」不能靠明度差：--bg-sunken 在页底上只有 1.02:1，等于没画。
   改用 --bg-panel 填充 + --border-strong 描边（1.57:1）才能看见容器边界。 */
.flow-nav {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  flex-shrink: 0;
  box-shadow: var(--card-sheen);
}

.flow-step {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 14px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: 999px;
  background: transparent;
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  line-height: 1;
  color: var(--text-secondary);
  cursor: pointer;
  transition: color 0.15s ease, background-color 0.15s ease, border-color 0.15s ease;
}

.flow-step:hover:not(.blocked):not(.active) {
  background: var(--overlay-hover);
  color: var(--text-primary);
}

/* 当前页：品牌渐变填充 + 深色文字。这是「我在哪一页」，必须一眼可见 */
.flow-step.active {
  background: var(--brand-gradient);
  border-color: transparent;
  color: var(--text-on-brand);
  font-weight: 600;
}

.flow-step:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

/* 不可达：降到禁用档并去 cursor 暗示，避免用户以为能点 */
.flow-step.blocked {
  color: var(--text-disabled);
  cursor: not-allowed;
}

/* 已完成：对勾用成功色；激活态里对勾跟随反色文字，不再额外着色 */
.flow-step.done:not(.active) .step-check {
  color: var(--state-success);
}
.step-check {
  display: inline-flex;
  align-items: center;
}

@media (max-width: 768px) {
  .flow-step {
    padding: 0 10px;
  }
}
</style>
