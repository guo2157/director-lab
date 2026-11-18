<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import logoUrl from '@/assets/logo.svg'
import { useRoute, useRouter } from 'vue-router'
import { NLayout, NLayoutHeader, NLayoutContent } from 'naive-ui'
import { useAgentStore } from '@/stores/agent'
import { useQuery } from '@tanstack/vue-query'
import { getProject } from '@/api/projects'
import { onWorkspaceEvent, startWorkspaceEvents } from '@/composables/useWorkspaceEvents'
import { initContextRevision, setContextConflictHandler } from '@/services/workspaceNavigation'
import type { WorkspaceEvent } from '@/api/workspace'
import ProjectProgressRail from '@/components/ProjectProgressRail.vue'
import DirectorAgentPanel from '@/components/DirectorAgentPanel.vue'

const route = useRoute()
const router = useRouter()
const agent = useAgentStore()

// ── 工作台事件流（V2 Phase 3）──
// 在应用外壳层启动一次，与任何业务页面的挂载/卸载无关（任务不依赖页面生命周期）。
// 这里只做两件事：启动连接 + 订阅导航事件回写 store；不承载业务状态。
let offWorkspaceEvent: (() => void) | null = null
onMounted(() => {
  startWorkspaceEvents()
  // 页面上下文版本号初始化：刷新后本地从 0 开始会必然冲突，启动时读取服务端当前值
  void initContextRevision()
  // 冲突回调：如实提示，不静默覆盖服务端上下文
  setContextConflictHandler(({ currentRevision, expectedRevision }) => {
    agent.navNotice =
      `页面上下文已被更新（本地 ${expectedRevision} / 服务端 ${currentRevision}），已同步最新版本，操作未丢失`
  })
  offWorkspaceEvent = onWorkspaceEvent((ev: WorkspaceEvent) => {
    // 导航事件：仅在本端尚未由 chat 响应写入提示时兜底展示，不重复提示
    if (ev.event_type === 'workspace.navigate' && !agent.navNotice && ev.route_name) {
      agent.navNotice = `已按 Agent 指令切换到「${ev.route_name}」`
    }
  })
})
onUnmounted(() => {
  // 只解除本组件订阅，不关闭全局连接（其他消费者仍在用）
  offWorkspaceEvent?.()
  offWorkspaceEvent = null
  setContextConflictHandler(null)
})
/** 助手面板是否以文档流形式占用右侧空间（窄屏由面板自身改为浮层） */
const isAgentDocked = computed(() => agent.panelOpen)

const projectId = computed(() => route.params.id as string | undefined)
/** 仅项目级路由才视为项目页；系列相关路由同样带 id 参数，不能误判 */
const PROJECT_ROUTE_NAMES = [
  'script',
  'parse',
  'storyboard-images',
  'storyboard',
  'assets',
  'compose',
]
const isProjectPage = computed(
  () => Boolean(projectId.value) && PROJECT_ROUTE_NAMES.includes(String(route.name ?? '')),
)
/** 系列相关路由统一高亮「作品」入口 */
const SERIES_ROUTE_NAMES = ['series-list', 'series-create', 'series-detail', 'series-continue', 'episode-review']
/** 导演台为固定视口布局：页面级滚动条不滚动，改由页面内部面板各自滚动 */
const isFixedViewport = computed(() => route.name === 'director')

/** 全局导航入口：作品（含单集项目与系列）| 任务中心 | 设置；导演台入口有意移除，经右侧 AI 助手面板进入 */
const topMenuOptions = [
  { label: '作品', key: 'projects' },
  { label: '任务中心', key: 'jobs' },
  { label: '设置', key: 'settings' },
]

/** 作品页与项目内页面统一高亮「作品」入口 */
const activeKey = computed(() => {
  const name = String(route.name ?? '')
  if (isProjectPage.value || SERIES_ROUTE_NAMES.includes(name)) return 'projects'
  return name
})

function goMenu(key: string) {
  void router.push({ name: key })
}

/** 面包屑第二级：项目名（与各页面 ['project', id] 共用同一个 Query 缓存，不重复请求） */
const projectQuery = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value!),
  enabled: computed(() => isProjectPage.value),
})
const projectName = computed(() => projectQuery.data.value?.name ?? '')

function goProjects() {
  if (isProjectPage.value) void router.push('/')
}
</script>

<template>
  <n-layout class="app-layout">
    <n-layout-header bordered class="app-header">
      <div class="header-inner">
        <div class="brand" @click="router.push('/')">
          <img class="brand-mark" :src="logoUrl" alt="Director Lab" />
          <span class="brand-name">Director Lab</span>
        </div>

        <nav class="top-nav" aria-label="主导航">
          <button
            v-for="item in topMenuOptions"
            :key="item.key"
            type="button"
            class="nav-item"
            :class="{ 'is-active': activeKey === item.key }"
            @click="goMenu(item.key)"
          >
            {{ item.label }}
          </button>
        </nav>

        <div v-if="isProjectPage" class="crumb-bar">
          <button type="button" class="crumb-back" @click="goProjects()">项目列表</button>
          <template v-if="projectName">
            <span class="crumb-sep" aria-hidden="true"></span>
            <span class="crumb-current">{{ projectName }}</span>
          </template>
        </div>
      </div>
    </n-layout-header>

    <n-layout-content class="app-content" :class="{ 'is-fixed': isFixedViewport }">
      <div
        class="content-body"
        :class="{ 'has-agent': isProjectPage, 'is-agent-docked': isAgentDocked }"
      >
        <ProjectProgressRail v-if="isProjectPage && projectId" :project-id="projectId" />
        <div class="content-main">
          <!-- 页面导航由左侧「制作流程」竖列与页内 FlowNav 承担，不再重复挂载工作区竖列 -->
          <slot />
        </div>
        <DirectorAgentPanel v-if="route.name !== 'director'" />
      </div>
    </n-layout-content>
  </n-layout>
</template>

<style scoped>
.app-layout {
  height: 100vh;
  height: 100dvh;
}

.app-header {
  height: var(--app-header-height, 52px);
  display: flex;
  align-items: center;
  background: var(--bg-sunken);
  border-bottom: 1px solid var(--border-subtle);
}

.header-inner {
  display: flex;
  align-items: center;
  gap: 36px;
  width: 100%;
  max-width: 1680px;
  margin: 0 auto;
  padding: 0 24px 0 20px;
  min-width: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-shrink: 0;
  cursor: pointer;
  user-select: none;
  padding-right: 4px;
}

.brand-mark {
  width: 22px;
  height: 22px;
  display: block;
  filter: saturate(0.9);
}

.brand-name {
  font-family: var(--font-display);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text-primary);
  white-space: nowrap;
}

.top-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.top-nav::-webkit-scrollbar {
  display: none;
}

.nav-item {
  position: relative;
  flex-shrink: 0;
  appearance: none;
  background: none;
  border: 0;
  padding: 0 14px;
  height: var(--app-header-height, 52px);
  font-family: inherit;
  font-size: 14px;
  font-weight: 500;
  letter-spacing: 0.02em;
  color: var(--text-secondary);
  cursor: pointer;
  transition: color var(--transition-fast);
}

.nav-item::after {
  content: '';
  position: absolute;
  left: 14px;
  right: 14px;
  bottom: -1px;
  height: 1px;
  background: transparent;
  transition: background var(--transition-fast);
}

.nav-item:hover {
  color: var(--text-primary);
}

.nav-item.is-active {
  color: var(--text-primary);
  font-weight: 600;
}

.nav-item.is-active::after {
  background: var(--brand-primary);
}

.crumb-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  min-width: 0;
  padding-left: 18px;
  border-left: 1px solid var(--border-subtle);
}

.crumb-back {
  appearance: none;
  background: none;
  border: 0;
  padding: 0;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  transition: color var(--transition-fast);
}

.crumb-back:hover {
  color: var(--brand-hover);
}

.crumb-sep {
  width: 1px;
  height: 12px;
  background: var(--border-strong);
}

.crumb-current {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-content {
  background: var(--bg-base);
  height: calc(100vh - var(--app-header-height, 52px));
  height: calc(100dvh - var(--app-header-height, 52px));
  overflow-y: auto;
}

.content-body {
  display: flex;
  min-height: 100%;
  width: 100%;
  max-width: 1680px;
  margin: 0 auto;
}

.content-main {
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: hidden;
}
/* 助手面板展开时：面板占据自身宽度，主内容只收缩可用部分，左侧轨道与起始位置保持不变 */
.content-body.is-agent-docked .content-main {
  transition: flex-basis var(--transition-base);
}
/* 非项目页内容区高度自适应：面板吸顶并限制在视口高度内，内部自行滚动 */
.content-body:not(.has-agent) :deep(.agent-drawer) {
  position: sticky;
  top: 0;
  align-self: flex-start;
  height: calc(100dvh - var(--app-header-height, 52px));
}
/* 右侧常驻 Agent 面板：内容区高度锁定，滚动交给 .content-main */
.content-body.has-agent {
  height: 100%;
  min-height: 0;
  overflow: hidden;
}
.content-body.has-agent .content-main {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}


.app-content.is-fixed {
  overflow: hidden;
}
.app-content.is-fixed .content-body {
  height: 100%;
  overflow: hidden;
}
.app-content.is-fixed .content-main {
  height: 100%;
  overflow: hidden;
}

@media (max-width: 768px) {
  .header-inner {
    gap: 8px;
    padding: 0 10px;
  }
  .brand-name {
    font-size: 14px;
  }
  .nav-item {
    padding: 0 7px;
    font-size: 13px;
  }
  .nav-item::after {
    left: 7px;
    right: 7px;
  }
  .crumb-sep,
  .crumb-current {
    display: none;
  }
  .content-body {
    flex-direction: column;
  }
}
@media (max-width: 768px) {
  .content-body.has-agent {
    height: auto;
    overflow: visible;
  }
  .content-body.has-agent .content-main {
    height: auto;
    overflow-y: visible;
  }
  /* 窄屏下助手为全屏浮层，不参与文档流占位 */
  .content-body.is-agent-docked .content-main {
    transition: none;
  }
}
</style>
