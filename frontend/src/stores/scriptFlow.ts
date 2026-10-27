import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { ScriptEstimate } from '../types/script'

/**
 * 剧本流程共享状态 store
 * 跨页面共享：ScriptView → EstimateView → ParseView
 * 流程：剧本保存 → 评估展示 → 解析落库（自动确认当前版本）
 */
export const useScriptFlowStore = defineStore('scriptFlow', () => {
  // ---------- 项目归属（切换项目时重置，避免跨项目状态污染） ----------
  const projectId = ref<string | null>(null)

  // ---------- 剧本 ----------
  const scriptVersionId = ref<string | null>(null)
  const scriptText = ref('')

  // ---------- 评估（完成后自动确认制作范围，后端解析前置条件） ----------
  const estimate = ref<ScriptEstimate | null>(null)
  const estimateId = ref<string | null>(null)

  // ---------- 选中集（评估页勾选，解析页严格按选中解析） ----------
  const selectedEpisodes = ref<number[]>([])
  function toggleEpisode(ep: number) {
    const i = selectedEpisodes.value.indexOf(ep)
    if (i >= 0) selectedEpisodes.value.splice(i, 1)
    else selectedEpisodes.value.push(ep)
    selectedEpisodes.value.sort((a, b) => a - b)
  }
  function selectAllEpisodes(total: number) {
    selectedEpisodes.value = Array.from({ length: total }, (_, i) => i + 1)
  }
  /** 将选中集拆分为连续区间 [start, end][] */
  const selectedRanges = computed(() => {
    const eps = selectedEpisodes.value
    if (!eps.length) return [] as Array<{ start: number; end: number }>
    const ranges: Array<{ start: number; end: number }> = []
    let s = eps[0]
    let prev = eps[0]
    for (let i = 1; i < eps.length; i++) {
      if (eps[i] === prev + 1) { prev = eps[i]; continue }
      ranges.push({ start: s, end: prev })
      s = eps[i]
      prev = eps[i]
    }
    ranges.push({ start: s, end: prev })
    return ranges
  })

  // ---------- 解析 ----------
  const parseJobId = ref<string | null>(null)
  const candidateVersionId = ref<string | null>(null)
  // 解析已确认（Agent 一体化流程完成后置位，供 FlowNav 显示解析完成态）
  const parseConfirmed = ref(false)

  // ---------- 派生 ----------
  const totalEpisodes = computed(() => estimate.value?.estimated_episodes ?? 0)
  const canParse = computed(
    () => !!scriptVersionId.value && !!estimate.value && !candidateVersionId.value,
  )

  // ---------- 重置 ----------
  /** 项目切换时重置流程状态：防止上一个项目的剧本/评估状态污染新项目 */
  function ensureProject(id: string) {
    if (projectId.value === id) return
    projectId.value = id
    scriptVersionId.value = null
    scriptText.value = ''
    estimate.value = null
    estimateId.value = null
    selectedEpisodes.value = []
    parseJobId.value = null
    candidateVersionId.value = null
    parseConfirmed.value = false
  }

  function resetAfterSave() {
    estimate.value = null
    estimateId.value = null
    parseJobId.value = null
    candidateVersionId.value = null
    parseConfirmed.value = false
    selectedEpisodes.value = []
  }

  function resetAfterConfirmParse() {
    parseJobId.value = null
    candidateVersionId.value = null
    parseConfirmed.value = true
  }

  return {
    projectId,
    ensureProject,
    scriptVersionId,
    scriptText,
    estimate,
    estimateId,
    selectedEpisodes,
    toggleEpisode,
    selectAllEpisodes,
    selectedRanges,
    parseJobId,
    candidateVersionId,
    parseConfirmed,
    totalEpisodes,
    canParse,
    resetAfterSave,
    resetAfterConfirmParse,
  }
})
