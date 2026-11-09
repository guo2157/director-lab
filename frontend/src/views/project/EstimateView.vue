<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NEmpty, NSpin, useMessage } from 'naive-ui'
import { confirmEstimate, estimateScript, getCurrentScript, getProject } from '@/api/projects'
import type { ScriptEstimate } from '@/types/script'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import FlowNav from '@/components/FlowNav.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const flow = useScriptFlowStore()

const projectId = computed(() => String(route.params.id ?? ''))

/* 项目切换时重置流程状态（防止其他项目残留的剧本/评估状态误显示对号） */
watch(
  projectId,
  (id) => {
    if (id) flow.ensureProject(id)
  },
  { immediate: true },
)

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) {
    const m = (e as { message?: string }).message
    if (m) return m
  }
  return e instanceof Error ? e.message : '请求失败'
}

/* ---------- 项目信息 ---------- */
const { data: projectData } = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
  enabled: computed(() => !!projectId.value),
})
const project = computed(() => projectData.value)

/* ---------- 剧本版本（刷新恢复） ---------- */
const { data: scriptData } = useQuery({
  queryKey: computed(() => ['currentScript', projectId.value]),
  queryFn: () => getCurrentScript(projectId.value),
  enabled: computed(() => !!projectId.value),
})
watch(
  () => scriptData.value,
  (sv) => {
    if (sv?.exists && sv.script_version_id) {
      flow.scriptVersionId = sv.script_version_id
      flow.scriptText = sv.text ?? ''
    }
  },
  { immediate: true },
)

/* ---------- 评估（仅展示，无需确认锁定） ---------- */
const estimateElapsed = ref(0)
let elapsedTimer: ReturnType<typeof setInterval> | null = null
function ensureElapsedTimer() {
  if (elapsedTimer) return
  elapsedTimer = setInterval(() => { estimateElapsed.value++ }, 1000)
}
function stopElapsedTimer() {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null }
}

const { mutate: runEstimate, isPending: estimating } = useMutation({
  mutationFn: () =>
    estimateScript(projectId.value, flow.scriptVersionId!, flow.scriptText),
  onMutate: () => {
    estimateElapsed.value = 0
    flow.selectedEpisodes.length = 0
    ensureElapsedTimer()
  },
  onSuccess: (est) => {
    flow.estimate = est as ScriptEstimate
    flow.estimateId = est.estimate_id
    /* 默认不勾选任何集，由用户自行选择要解析的集数 */
    flow.selectedEpisodes.length = 0
    stopElapsedTimer()
    // 后端解析前置条件：评估后须确认制作范围（默认全部集数），此处自动确认
    confirmEstimateMut()
  },
  onError: (e) => {
    stopElapsedTimer()
    message.error(errMsg(e))
  },
})

/* ---------- 确认制作范围（默认全部集数，满足解析前置条件） ---------- */
const { mutate: confirmEstimateMut } = useMutation({
  mutationFn: () =>
    confirmEstimate(projectId.value, flow.estimateId!, {
      script_version_id: flow.scriptVersionId!,
      episode_start: 1,
      episode_count: flow.estimate?.estimated_episodes ?? 1,
    }),
  onSuccess: () => {},
  onError: (e) => message.error(`制作范围确认失败：${errMsg(e)}`),
})


/* ---------- 自动发起评估 ---------- */
watch(
  () => flow.scriptVersionId,
  (vid) => {
    if (vid && !flow.estimate && !estimating.value) {
      runEstimate()
    }
  },
  { immediate: true },
)
</script>

<template>
  <section class="estimate-page">
    <header class="page-header">
      <div class="header-left">
        <h1 class="page-title">评估</h1>
        <FlowNav :project-id="projectId" />
        <span class="project-name">{{ project?.name ?? '' }}</span>
      </div>
      <div class="header-actions">
        <NButton size="small" @click="router.push(`/project/${projectId}/script`)">返回剧本</NButton>
        <NButton
          v-if="flow.estimate"
          size="small"
          type="primary"
          :disabled="!flow.selectedEpisodes.length"
          @click="router.push(`/project/${projectId}/parse`)"
        >解析（{{ flow.selectedEpisodes.length }} 集）</NButton>
      </div>
    </header>
    <div class="page-body">
      <div class="content-pane">
        <NSpin v-if="estimating" size="medium" class="page-spin" />
        <template v-else-if="flow.estimate">
          <div class="estimate-grid">
            <div class="estimate-stat">
              <span class="estimate-stat-label">总集数</span>
              <span class="estimate-stat-value-lg">{{ flow.estimate.estimated_episodes }}</span>
            </div>
            <div class="estimate-stat">
              <span class="estimate-stat-label">预计时长</span>
              <span class="estimate-stat-value">{{ flow.estimate.estimated_total_duration ?? '—' }}min</span>
            </div>
            <div class="estimate-stat">
              <span class="estimate-stat-label">剧本字数</span>
              <span class="estimate-stat-value">{{ flow.scriptText.length }}</span>
            </div>
            <div class="estimate-stat">
              <span class="estimate-stat-label">对白字数</span>
              <span class="estimate-stat-value">{{ flow.estimate.dialogue_char_estimate }}</span>
            </div>
          </div>
          <div v-if="flow.estimate.episode_breakdown?.length" class="episode-breakdown">
            <span class="pane-label">分集预估</span>
            <div class="breakdown-list">
              <div v-for="ep in flow.estimate.episode_breakdown" :key="ep.episode" class="breakdown-item">
                <span class="breakdown-ep">第 {{ ep.episode }} 集</span>
                <span class="breakdown-meta">约 {{ ep.estimated_shots }} 镜头 · {{ ep.estimated_duration }}s</span>
              </div>
            </div>
          </div>
          <div v-if="flow.estimate.estimated_episodes" class="episode-selector">
            <span class="pane-label">选择解析集（点击切换）</span>
            <div class="episode-select-grid">
              <button
                v-for="ep in flow.estimate.estimated_episodes"
                :key="ep"
                class="episode-select-cell"
                :class="{ selected: flow.selectedEpisodes.includes(ep) }"
                @click="flow.toggleEpisode(ep)"
              >
                <span v-if="flow.selectedEpisodes.includes(ep)">✓</span>
                <span v-else>{{ ep }}</span>
              </button>
            </div>
          </div>
          <p v-if="flow.estimate.suggestion" class="estimate-suggestion">{{ flow.estimate.suggestion }}</p>
          <div class="estimate-actions">
            <NButton
              type="primary"
              :disabled="!flow.selectedEpisodes.length"
              @click="router.push(`/project/${projectId}/parse`)"
            >解析（{{ flow.selectedEpisodes.length }} 集）</NButton>
          </div>
        </template>
        <NEmpty v-else description="请先保存剧本版本" />
      </div>
    </div>
  </section>
</template>

<style scoped>
.estimate-page { display: flex; flex-direction: column; height: 100%; overflow: hidden; }
.page-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 16px; border-bottom: 1px solid var(--border-subtle, #e5e7eb); flex-shrink: 0; }
.header-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.page-title { font-size: 16px; font-weight: 600; margin: 0; }
.project-name { font-size: 12px; color: var(--text-secondary, #64748b); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.page-body { display: flex; flex: 1; min-height: 0; }
.content-pane { flex: 1; padding: 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px; }
.page-spin { align-self: center; margin-top: 80px; }
.estimate-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.estimate-stat { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 12px; padding: 16px; display: flex; flex-direction: column; gap: 8px; background: var(--bg-panel, #fff); }
.estimate-stat-label { font-size: 12px; color: var(--text-secondary, #64748b); }
.estimate-stat-value { font-size: 22px; font-weight: 700; color: var(--text-primary, #333); }
.estimate-stat-value-lg { font-size: 26px; font-weight: 700; color: var(--brand-primary); }
.pane-label { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); }
.episode-breakdown { display: flex; flex-direction: column; gap: 8px; }
.breakdown-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
.breakdown-item { border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 8px; padding: 8px 12px; display: flex; flex-direction: column; gap: 4px; }
.breakdown-ep { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); }
.breakdown-meta { font-size: 12px; color: var(--text-secondary, #64748b); }
.estimate-suggestion { font-size: 13px; color: var(--text-secondary, #64748b); background: var(--bg-elevated, #f5f5f5); border-radius: 8px; padding: 10px 12px; margin: 0; }
.estimate-actions { display: flex; gap: 12px; }
.episode-selector { display: flex; flex-direction: column; gap: 10px; }
.episode-select-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(44px, 1fr)); gap: 8px; }
.episode-select-cell { width: 100%; aspect-ratio: 1; border: 2px solid var(--border-subtle, #e5e7eb); border-radius: 8px; background: var(--bg-panel, #fff); font-size: 14px; font-weight: 600; color: var(--text-primary, #333); cursor: pointer; display: flex; align-items: center; justify-content: center; transition: all 0.15s ease; }
.episode-select-cell:hover { border-color: var(--brand-primary); }
.episode-select-cell.selected { border-color: var(--brand-primary); background: rgba(53, 179, 166, 0.12); color: var(--brand-primary); }
@media (max-width: 768px) { .estimate-grid { grid-template-columns: repeat(2, 1fr); } .page-header { flex-wrap: wrap; } }
</style>
