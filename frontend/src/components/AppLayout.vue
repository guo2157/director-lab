<script setup lang="ts">
import { computed } from 'vue'
import logoUrl from '@/assets/logo.svg'
import { useRoute, useRouter } from 'vue-router'
import { NLayout, NLayoutHeader, NLayoutContent, NMenu, NButton, NTooltip } from 'naive-ui'
import type { MenuOption } from 'naive-ui'
import ProjectProgressRail from '@/components/ProjectProgressRail.vue'

const route = useRoute()
const router = useRouter()

const projectId = computed(() => route.params.id as string | undefined)
const isProjectPage = computed(() => Boolean(projectId.value))

const topMenuOptions: MenuOption[] = [
  { label: '导演台', key: 'director' },
  { label: '项目', key: 'projects' },
  { label: '任务中心', key: 'jobs' },
  { label: '设置', key: 'settings' },
]

const projectMenuOptions: MenuOption[] = [
  { label: '剧本', key: 'script' },
  { label: '资产库', key: 'assets' },
  // 分镜图：资产库人工审核通过后进入，逐镜出图并人工确认
  { label: '分镜图', key: 'storyboard-images' },
  { label: '分镜看板', key: 'storyboard' },
  { label: '合成与导出', key: 'compose' },
]

const activeKey = computed(() => route.name as string)

function handleMenuSelect(key: string) {
  if (isProjectPage.value) {
    router.push(`/project/${projectId.value}/${key}`)
  } else {
    router.push({ name: key })
  }
}

const menuOptions = computed(() => (isProjectPage.value ? projectMenuOptions : topMenuOptions))

/** 当前页面标题（用于面包屑末级展示） */
const currentTitle = computed(() => String(route.meta?.title ?? ''))

/** 返回项目列表（首页） */
function goProjects() {
  if (isProjectPage.value) router.push('/')
}
</script>

<template>
  <n-layout class="app-layout">
    <n-layout-header bordered class="app-header">
      <div class="header-inner">
        <div class="logo" @click="router.push('/')">
          <img class="logo-icon" :src="logoUrl" alt="Director Lab logo" />
          <span class="logo-text">Director Lab</span>
        </div>
        <!-- 项目内页面：返回项目列表入口 + 面包屑，明确当前位置与出口 -->
        <div v-if="isProjectPage" class="crumb-bar">
          <n-tooltip trigger="hover">
            <template #trigger>
              <n-button size="small" quaternary class="back-btn" @click="goProjects()">
                ← 项目列表
              </n-button>
            </template>
            返回项目列表（首页）
          </n-tooltip>
          <span class="crumb-sep">/</span>
          <span class="crumb-current">{{ currentTitle }}</span>
        </div>
        <n-menu
          class="header-menu"
          mode="horizontal"
          :options="menuOptions"
          :value="activeKey"
          @update:value="handleMenuSelect"
        />
      </div>
    </n-layout-header>
    <n-layout-content class="app-content">
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
  height: var(--app-header-height, 56px);
  display: flex;
  align-items: center;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-subtle);
}
.header-inner {
  display: flex;
  align-items: center;
  gap: 32px;
  width: 100%;
  max-width: 1600px;
  margin: 0 auto;
  padding: 0 24px;
  min-width: 0;
}
.logo {
  cursor: pointer;
  user-select: none;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}
.logo-icon {
  width: 28px;
  height: 28px;
  display: block;
}
.logo-text {
  font-size: 16px;
  font-weight: 700;
  color: var(--brand-primary);
}
.header-menu {
  flex: 1;
  min-width: 0;
}
/* 顶部导航菜单字体加大并加粗（Naive UI 默认 14px/400），提升辨识度 */
.header-menu :deep(.n-menu-item-content) {
  font-size: 17px;
}
.header-menu :deep(.n-menu-item-content .n-menu-item-content-header) {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.3px;
}
.crumb-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  min-width: 0;
}
.back-btn {
  font-size: 14px;
  color: var(--text-secondary);
}
.back-btn:hover {
  color: var(--brand-primary);
}
.crumb-sep {
  font-size: 14px;
  color: var(--text-tertiary);
}
.crumb-current {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.app-content {
  background: var(--bg-base);
  height: calc(100vh - var(--app-header-height, 56px));
  height: calc(100dvh - var(--app-header-height, 56px));
  overflow-y: auto;
}
.content-body {
  display: flex;
  min-height: 100%;
  width: 100%;
  max-width: 1600px;
  margin: 0 auto;
}
.content-main {
  flex: 1;
  min-width: 0;
}

@media (max-width: 768px) {
  .header-inner {
    gap: 12px;
    padding: 0 12px;
  }
  .logo-text {
    font-size: 14px;
  }
  .header-menu {
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .header-menu::-webkit-scrollbar {
    display: none;
  }
  .header-menu :deep(.n-menu-item),
  .header-menu :deep(.n-menu-item-content) {
    flex-shrink: 0;
  }
  .header-menu :deep(.n-menu-item-content),
  .header-menu :deep(.n-menu-item-content .n-menu-item-content-header) {
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
  }
  .content-body {
    flex-direction: column;
  }
  /* 窄屏优先保证返回入口可见，隐藏面包屑末级文字 */
  .crumb-sep,
  .crumb-current {
    display: none;
  }
}
</style>
