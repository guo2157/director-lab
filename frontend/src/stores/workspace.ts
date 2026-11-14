import { defineStore } from 'pinia'
import { ref, computed, shallowRef } from 'vue'

/**
 * 双栏工作台布局状态（V2 导演工作台 Phase 1）
 * 职责：分栏尺寸、折叠、定位（当前路由/选中资产等 UI 状态）
 * 不存 Agent 会话数据（AgentState 走 TanStack Query）
 * 不存任务进度（TaskState 走 SSE + TanStack Query）
 */
export const useWorkspaceStore = defineStore('workspace', () => {
  // —— 布局状态 ——
  const leftWidth = ref(320)
  const leftMinWidth = ref(240)
  const leftMaxWidth = ref(600)
  const leftCollapsed = ref(false)
  const _leftWidthBeforeCollapse = ref(320)

  function collapseLeft() {
    if (!leftCollapsed.value) {
      _leftWidthBeforeCollapse.value = leftWidth.value
      leftWidth.value = 0
      leftCollapsed.value = true
    }
  }

  function expandLeft() {
    if (leftCollapsed.value) {
      leftWidth.value = _leftWidthBeforeCollapse.value
      leftCollapsed.value = false
    }
  }

  const isLeftCollapsed = computed(() => leftCollapsed.value)

  function setLeftWidth(w: number) {
    leftWidth.value = Math.max(leftMinWidth.value, Math.min(leftMaxWidth.value, w))
    leftCollapsed.value = false
  }

  // —— 定位状态 ——
  const activeRoute = ref<string | null>(null)
  const selectedEntityId = shallowRef<string | null>(null)
  const selectedEntityType = ref<string | null>(null)

  function selectEntity(entityId: string, entityType: string) {
    selectedEntityId.value = entityId
    selectedEntityType.value = entityType
  }

  function clearSelection() {
    selectedEntityId.value = null
    selectedEntityType.value = null
  }

  function setActiveRoute(name: string) {
    activeRoute.value = name
  }

  // —— 全屏模式 ——
  const isFullscreen = ref(false)

  function toggleFullscreen() {
    isFullscreen.value = !isFullscreen.value
  }

  return {
    leftWidth, leftMinWidth, leftMaxWidth, leftCollapsed, isLeftCollapsed,
    collapseLeft, expandLeft, setLeftWidth,
    activeRoute, selectedEntityId, selectedEntityType,
    selectEntity, clearSelection, setActiveRoute,
    isFullscreen, toggleFullscreen,
  }
})
