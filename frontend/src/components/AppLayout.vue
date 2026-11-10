<script setup lang="ts">
import { computed } from 'vue'
import logoUrl from '@/assets/logo.svg'
import { useRoute, useRouter } from 'vue-router'
import { NLayout, NLayoutHeader, NLayoutContent } from 'naive-ui'
import ProjectProgressRail from '@/components/ProjectProgressRail.vue'

const route = useRoute()
const router = useRouter()

const projectId = computed(() => route.params.id as string | undefined)
/** 仅项目级路由才视为项目页；系列相关路由同样带 id 参数，不能误判 */
const PROJECT_ROUTE_NAMES = [
  'script',
  'estimate',
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

/** 全局导航入口：导演台 | 作品（含单集项目与系列）| 任务中心 | 设置 */
const topMenuOptions = [
  { label: '导演台', key: 'director' },
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

const currentTitle = computed(() => String(route.meta?.title ?? ''))

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
          <span class="crumb-sep" aria-hidden="true"></span>
          <span class="crumb-current">{{ currentTitle }}</span>
        </div>
      </div>
    </n-layout-header>

    <n-layout-content class="app-content" :class="{ 'is-fixed': isFixedViewport }">
      <div class="content-body">
        <ProjectProgressRail v-if="isProjectPage && projectId" :project-id="projectId" />
        <div class="content-main">
          <slot />
        </div>
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
  flex: 1;
  min-width: 0;
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
    gap: 14px;
    padding: 0 12px;
  }
  .brand-name {
    font-size: 14px;
  }
  .nav-item {
    padding: 0 10px;
    font-size: 13px;
  }
  .nav-item::after {
    left: 10px;
    right: 10px;
  }
  .crumb-sep,
  .crumb-current {
    display: none;
  }
  .content-body {
    flex-direction: column;
  }
}
</style>
