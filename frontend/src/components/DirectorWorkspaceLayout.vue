<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspace'

const route = useRoute()
const router = useRouter()
const ws = useWorkspaceStore()

/** 需要双栏布局的路由白名单（与 AppLayout PROJECT_ROUTE_NAMES 同步） */
const WORKSPACE_ROUTE_NAMES = new Set([
  'script',
  'estimate',
  'parse',
  'storyboard-images',
  'storyboard',
  'assets',
  'compose',
])

const isWorkspacePage = computed(() =>
  WORKSPACE_ROUTE_NAMES.has(String(route.name ?? ''))
)

/** 当前路由名作为左侧导航高亮 */
watch(
  () => route.name,
  (name) => {
    if (name) ws.setActiveRoute(String(name))
  },
  { immediate: true }
)

/** 工作区页面页签（与 PROJECT_ROUTE_NAMES 对应） */
const pageTabs = [
  { key: 'script', label: '剧本' },
  { key: 'estimate', label: '评估' },
  { key: 'parse', label: '解析' },
  { key: 'storyboard-images', label: '分镜图' },
  { key: 'storyboard', label: '分镜看板' },
  { key: 'assets', label: '资产库' },
  { key: 'compose', label: '合成导出' },
]

function goPage(key: string) {
  const projectId = route.params.id as string | undefined
  if (projectId) {
    void router.push({ name: key, params: { id: projectId } })
  }
}


/** 拖拽调整左侧栏宽度 */
function onResizeStart(e: MouseEvent) {
  const startX = e.clientX
  const startWidth = ws.leftWidth

  function onMove(ev: MouseEvent) {
    const delta = ev.clientX - startX
    ws.setLeftWidth(startWidth + delta)
  }

  function onUp() {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }

  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}
</script>

<template>
  <div v-if="isWorkspacePage" class="workspace-layout">
    <!-- 左侧页面导航栏 -->
    <aside
      class="workspace-sidebar"
      :class="{ collapsed: ws.isLeftCollapsed }"
      :style="{ width: ws.leftWidth > 0 ? ws.leftWidth + 'px' : undefined }"
    >
      <div class="sidebar-header">
        <span class="sidebar-title">工作区</span>
        <button
          type="button"
          class="sidebar-toggle"
          :title="ws.isLeftCollapsed ? '展开侧栏' : '收起侧栏'"
          @click="ws.isLeftCollapsed ? ws.expandLeft() : ws.collapseLeft()"
        >
          {{ ws.isLeftCollapsed ? '▶' : '◀' }}
        </button>
      </div>

      <nav class="sidebar-nav" v-show="!ws.isLeftCollapsed">
        <button
          v-for="tab in pageTabs"
          :key="tab.key"
          type="button"
          class="nav-tab"
          :class="{ active: ws.activeRoute === tab.key }"
          @click="goPage(tab.key)"
        >
          {{ tab.label }}
        </button>
      </nav>
    </aside>

    <!-- 拖拽分隔条 -->
    <div
      v-if="!ws.isLeftCollapsed"
      class="workspace-resizer"
      @mousedown="onResizeStart"
    />

    <!-- 右侧主内容区 -->
    <main class="workspace-main">
      <slot />
    </main>
  </div>

  <!-- 非工作区页面直接透传 -->
  <div v-else class="workspace-fallback">
    <slot />
  </div>
</template>

<style scoped>
.workspace-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

.workspace-sidebar {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-sunken);
  border-right: 1px solid var(--border-subtle);
  transition: width var(--transition-fast);
  overflow: hidden;
}

.workspace-sidebar.collapsed {
  width: 0 !important;
  border-right: none;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}

.sidebar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
}

.sidebar-toggle {
  appearance: none;
  background: none;
  border: 0;
  padding: 2px 6px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  border-radius: 4px;
  transition: color var(--transition-fast), background var(--transition-fast);
}

.sidebar-toggle:hover {
  color: var(--text-primary);
  background: var(--bg-hover);
}

.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.nav-tab {
  appearance: none;
  background: none;
  border: 0;
  padding: 8px 12px;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-secondary);
  text-align: left;
  cursor: pointer;
  border-radius: 6px;
  transition: background var(--transition-fast), color var(--transition-fast);
  white-space: nowrap;
}

.nav-tab:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.nav-tab.active {
  background: var(--brand-subtle);
  color: var(--brand-primary);
  font-weight: 600;
}

.workspace-resizer {
  width: 4px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  transition: background var(--transition-fast);
}

.workspace-resizer:hover {
  background: var(--brand-primary);
}

.workspace-main {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  overflow-x: hidden;
}

.workspace-fallback {
  height: 100%;
  overflow-y: auto;
}

@media (max-width: 768px) {
  .workspace-sidebar {
    position: fixed;
    left: 0;
    top: var(--app-header-height, 52px);
    bottom: 0;
    z-index: 100;
    box-shadow: 4px 0 16px rgba(0,0,0,.15);
  }

  .workspace-sidebar.collapsed {
    display: none;
  }

  .workspace-resizer {
    display: none;
  }

  .workspace-main {
    width: 100%;
  }
}
</style>
