<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import logoUrl from '@/assets/logo.svg'
import { useRoute, useRouter } from 'vue-router'
import { NLayout, NLayoutContent } from 'naive-ui'
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

/** 全局导航入口：作品（含单集项目与系列）| 设置；导演台入口有意移除，经右侧 AI 助手面板进入。
 *  任务中心已下线（2026-11-19）：任务进度改为在产出它的页面内联展示（分镜图/资产/剧本页自带进度），
 *  不再需要一个独立的全局任务列表页。 */
const topMenuOptions = [
  { label: '作品', key: 'projects' },
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
    <!-- 顶部品牌渐变细线：整页唯一常驻的装饰，标记「这是一个产品」 -->
    <div class="top-hairline" aria-hidden="true" />

    <header class="app-header">
      <div class="header-pill">
        <button type="button" class="brand" aria-label="返回作品列表" @click="router.push('/')">
          <img class="brand-mark" :src="logoUrl" alt="" />
          <span class="brand-name">Director Lab</span>
        </button>

        <nav class="top-nav" aria-label="主导航">
          <button
            v-for="item in topMenuOptions"
            :key="item.key"
            type="button"
            class="nav-item"
            :class="{ 'is-active': activeKey === item.key }"
            :aria-current="activeKey === item.key ? 'page' : undefined"
            @click="goMenu(item.key)"
          >
            {{ item.label }}
          </button>
        </nav>

        <div v-if="isProjectPage" class="crumb-bar">
          <button type="button" class="crumb-back" @click="goProjects()">作品</button>
          <template v-if="projectName">
            <span class="crumb-sep" aria-hidden="true">/</span>
            <span class="crumb-current">{{ projectName }}</span>
          </template>
        </div>
        <!-- 第三列占位：grid 1fr auto 1fr 必须三格齐全，导航才能真正落在容器几何中心 -->
        <div v-else class="header-spacer" aria-hidden="true" />
      </div>
    </header>

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
  background: var(--bg-base);
}

/* 顶部渐变发丝线：贴在视口最上沿，1px */
.top-hairline {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: 1px;
  z-index: 20;
  background: var(--brand-gradient-line);
  opacity: 0.55;
  pointer-events: none;
}

/* 悬浮胶囊导航：与页面底色分离，靠边框 + 半透明底建立「浮在内容之上」 */
.app-header {
  position: sticky;
  top: 0;
  z-index: 15;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 10px var(--sp-6) 0;
  background: linear-gradient(180deg, var(--bg-base) 62%, transparent 100%);
}

.header-pill {
  position: relative;
  display: grid;
  /* 必须是 minmax(0, 1fr) 而不是 1fr：1fr 的隐含最小值是 auto（= 内容的
     min-content 宽度），窄屏下品牌名的固有宽度会把左列撑宽、右列被压窄，
     中列导航随之整体右移。实测 768 偏 2px、390 偏 27px。
     用 minmax(0, 1fr) 后两列严格等宽，导航恒在几何中心。 */
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: var(--sp-4);
  width: 100%;
  max-width: 1440px;
  height: 52px;
  /* 左右 padding 必须对称：grid 1fr auto 1fr 下不对称 padding 会把中列整体推偏
     （实测 左16/右8 时导航中心比胶囊中心偏右 4px） */
  padding: 0 var(--sp-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  background: var(--bg-panel);
  box-shadow: var(--card-sheen);
  min-width: 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-shrink: 0;
  justify-self: start;
  min-width: 0;
  cursor: pointer;
  user-select: none;
  appearance: none;
  background: none;
  border: 0;
  padding: 0;
  color: inherit;
  font: inherit;
}

.brand-mark {
  width: 22px;
  height: 22px;
  display: block;
}

.brand-name {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: var(--text-primary);
  white-space: nowrap;
  /* 窄屏下品牌名不得顶破左列（左列已锁 minmax(0,1fr)），超长时省略 */
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 导航容器：只有两个入口时，两个裸胶囊会读成「一个按钮 + 一个标签」，
   未激活项几乎没有视觉重量。改为分段控件（轨道底 + 内嵌项），
   两项获得相同的面积，字号也才敢往上加。
   轨道走 --bg-sunken（比顶栏 --bg-panel 更深）而不是 --bg-elevated：
   分段控件的语义是「凹陷的槽」，比容器更亮的轨道会读成浮起的按钮块。 */
.top-nav {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  justify-self: center;
  min-width: 0;
  padding: 4px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  background: var(--bg-sunken);
  overflow-x: auto;
  scrollbar-width: none;
}
.top-nav::-webkit-scrollbar {
  display: none;
}

/* 导航项：激活态 = 品牌 soft 底 + 品牌字 + 内描边（三重编码）。
   字号走 15.5px 档（DESIGN.md 3 节卡片标题档），不再用 13.5px 的界面微调档：
   顶栏是全局唯一常驻导航，13.5px 在 52px 胶囊里读起来像辅助文字。
   未激活字色用 --text-secondary：实测在 --bg-sunken 上 8.99:1（远超 AA 4.5），
   且比 --text-primary（18.59:1）低一档，不会和激活态的青字抢焦点。 */
.nav-item {
  position: relative;
  flex-shrink: 0;
  appearance: none;
  background: none;
  border: 0;
  padding: 0 22px;
  height: 36px;
  font-family: var(--font-display);
  font-size: 15.5px;
  font-weight: 500;
  letter-spacing: -0.005em;
  color: var(--text-secondary);
  border-radius: var(--radius-pill);
  cursor: pointer;
  transition:
    color var(--transition-fast),
    background-color var(--transition-fast),
    box-shadow var(--transition-fast);
}

.nav-item:hover {
  color: var(--text-primary);
  background: var(--overlay-hover);
}

.nav-item.is-active {
  color: var(--brand-primary);
  background: var(--brand-soft);
  box-shadow: inset 0 0 0 1px var(--brand-line);
  font-weight: 600;
}

.crumb-bar {
  display: flex;
  align-items: center;
  justify-self: end;
  gap: 8px;
  flex-shrink: 0;
  min-width: 0;
  max-width: 320px;
  padding-left: var(--sp-4);
  border-left: 1px solid var(--border-subtle);
}

.crumb-back {
  appearance: none;
  background: none;
  border: 0;
  padding: 0;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: color var(--transition-fast);
  white-space: nowrap;
}

.crumb-back:hover {
  color: var(--brand-primary);
}

.header-spacer {
  min-width: 0;
}

.crumb-sep {
  color: var(--border-strong);
  font-size: 12px;
}

.crumb-current {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-content {
  background: var(--bg-base);
  height: calc(100vh - var(--app-header-height, 64px));
  height: calc(100dvh - var(--app-header-height, 64px));
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
/*
 * 右下角有 position:fixed 的「AI 助手」浮钮（bottom 24 + 高约 36）。
 * 可滚动页面必须预留避让，否则页面最后一个可点元素会被浮钮压住；
 * 固定视口页面（is-fixed）自己管理内部滚动区，不叠加，避免把内容挤窄。
 */
.app-content:not(.is-fixed) .content-main {
  padding-bottom: 72px;
}
/* 助手面板展开时：面板占据自身宽度，主内容只收缩可用部分，左侧轨道与起始位置保持不变 */
.content-body.is-agent-docked .content-main {
  transition: flex-basis var(--transition-base);
}
/* 非项目页内容区高度自适应：面板吸顶并限制在视口高度内，内部自行滚动。
   仅限宽屏——窄屏（<=768px）下面板是全屏浮层，由组件内的 position: fixed 接管；
   若此处不加媒体查询，本选择器的优先级会压过组件的窄屏规则，面板会退化成
   380px 宽的 sticky 块而不是全屏浮层。 */
@media (min-width: 769px) {
  .content-body:not(.has-agent) :deep(.agent-drawer) {
    position: sticky;
    top: 0;
    align-self: flex-start;
    height: calc(100dvh - var(--app-header-height, 64px));
  }
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
  .app-header {
    padding: 8px var(--sp-3) 0;
  }
  .header-pill {
    gap: var(--sp-3);
    height: 48px;
    /* 窄屏必须左右对称：原 0 var(--sp-2) 0 var(--sp-3) 会让中列再偏 2px */
    padding: 0 var(--sp-3);
  }
  /* 窄屏：品牌只留图标。
     左列被 minmax(0,1fr) 锁成 96px（390 视口），而「图标 + Director Lab」
     需要 124px —— 保留文字必然溢出 28px，只能截成「Director La...」，
     比不显示更糟。图标本身已足够识别，且品牌区在手机上不是导航路径。 */
  .brand-name {
    display: none;
  }
  .brand-mark {
    width: 20px;
    height: 20px;
  }
  /* 窄屏导航：字号只降一档（15.5 → 14.5），不再掉到 13px。
     顶栏在手机上仍是唯一导航，13px 加窄 padding 会读成次要文字。 */
  .nav-item {
    padding: 0 14px;
    height: 32px;
    font-size: 14.5px;
  }
  .top-nav {
    padding: 3px;
  }
  .crumb-bar {
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
