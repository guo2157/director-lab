import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Project } from '@/types/project'

/**
 * 当前选中项目 store（FRONTEND.md：Pinia 只存客户端状态）
 * 服务端项目数据一律走 TanStack Query，这里仅保存当前选中项目 ID 与轻量缓存
 */
export const useProjectStore = defineStore('project', () => {
  /** 当前选中项目 ID */
  const currentProjectId = ref<string | null>(null)

  /** 当前项目轻量缓存（用于侧栏/面包屑展示，权威数据以 TanStack Query 为准） */
  const currentProject = ref<Project | null>(null)

  /** 是否已选中项目 */
  const hasProject = computed(() => currentProjectId.value !== null)

  /** 选中项目（写入缓存） */
  function selectProject(project: Project) {
    currentProjectId.value = project.id
    currentProject.value = project
  }

  /** 仅记录 ID（详情由 Query 拉取后回填） */
  function selectProjectById(id: string) {
    currentProjectId.value = id
  }

  /** 更新缓存中的项目信息（Query 数据刷新时回填） */
  function patchProject(project: Project) {
    if (currentProjectId.value === project.id) {
      currentProject.value = project
    }
  }

  /** 退出项目 */
  function clearProject() {
    currentProjectId.value = null
    currentProject.value = null
  }

  return {
    currentProjectId,
    currentProject,
    hasProject,
    selectProject,
    selectProjectById,
    patchProject,
    clearProject,
  }
})
