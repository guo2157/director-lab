<script setup lang="ts">
import { computed } from 'vue'
import logoUrl from '@/assets/logo.svg'
import { useRoute, useRouter } from 'vue-router'
import { NLayout, NLayoutHeader, NLayoutContent, NMenu } from 'naive-ui'
import type { MenuOption } from 'naive-ui'

const route = useRoute()
const router = useRouter()

const projectId = computed(() => route.params.id as string | undefined)
const isProjectPage = computed(() => Boolean(projectId.value))

const topMenuOptions: MenuOption[] = [
  { label: '项目', key: 'projects' },
  { label: '任务中心', key: 'jobs' },
  { label: '设置', key: 'settings' },
]

const projectMenuOptions: MenuOption[] = [
  { label: '剧本', key: 'script' },
  { label: '分镜看板', key: 'storyboard' },
  { label: '资产库', key: 'assets' },
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
</script>

<template>
  <n-layout class="app-layout">
    <n-layout-header bordered class="app-header">
      <div class="header-inner">
        <div class="logo" @click="router.push('/')">
          <img class="logo-icon" :src="logoUrl" alt="Director Lab logo" />
          <span class="logo-text">Director Lab</span>
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
      <slot />
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
.app-content {
  background: var(--bg-base);
  height: calc(100vh - var(--app-header-height, 56px));
  height: calc(100dvh - var(--app-header-height, 56px));
  overflow-y: auto;
}

@media (max-width: 768px) {
  .header-inner {
    gap: 12px;
    padding: 0 12px;
  }
  .logo-text {
    font-size: 14px;
  }
}
</style>
