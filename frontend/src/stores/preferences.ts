import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

const STORAGE_KEY = 'director-lab.preferences'

interface PreferenceState {
  sidebarCollapsed: boolean
  sidebarAutoCollapseDismissed: boolean
}

function loadState(): PreferenceState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<PreferenceState>
      return {
        sidebarCollapsed: typeof parsed.sidebarCollapsed === 'boolean' ? parsed.sidebarCollapsed : false,
        sidebarAutoCollapseDismissed: parsed.sidebarAutoCollapseDismissed === true,
      }
    }
  } catch {
    // 忽略解析失败，使用默认值
  }
  return { sidebarCollapsed: false, sidebarAutoCollapseDismissed: false }
}

/**
 * 用户偏好 store（FRONTEND.md：Pinia 只存客户端状态）
 * 侧栏折叠状态持久化到 localStorage；<1440px 自动折叠时仅当用户未手动展开过
 */
export const usePreferencesStore = defineStore('preferences', () => {
  const initial = loadState()

  /** 侧栏是否折叠（220px ↔ 64px） */
  const sidebarCollapsed = ref(initial.sidebarCollapsed)

  /** 用户是否手动切换过折叠（手动操作优先于 <1440px 自动折叠） */
  const sidebarManuallyToggled = ref(initial.sidebarAutoCollapseDismissed)

  /** 切换侧栏折叠 */
  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
    sidebarManuallyToggled.value = true
  }

  /** 设置侧栏折叠（供 <1440px 自动折叠使用，不覆盖用户手动选择） */
  function setSidebarCollapsed(collapsed: boolean) {
    if (!sidebarManuallyToggled.value) {
      sidebarCollapsed.value = collapsed
    }
  }

  /** 重置为跟随自动折叠 */
  function resetSidebarPreference() {
    sidebarManuallyToggled.value = false
  }

  // 持久化
  watch(
    [sidebarCollapsed, sidebarManuallyToggled],
    () => {
      try {
        const state: PreferenceState = {
          sidebarCollapsed: sidebarCollapsed.value,
          sidebarAutoCollapseDismissed: sidebarManuallyToggled.value,
        }
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
      } catch {
        // 存储不可用时静默忽略
      }
    },
  )

  return {
    sidebarCollapsed,
    sidebarManuallyToggled,
    toggleSidebar,
    setSidebarCollapsed,
    resetSidebarPreference,
  }
})
