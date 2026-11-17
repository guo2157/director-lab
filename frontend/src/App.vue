<script setup lang="ts">
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui'
import AppLayout from '@/components/AppLayout.vue'
import GlobalConfirmDialog from '@/components/ui/GlobalConfirmDialog.vue'
import { buildThemeOverrides } from '@/styles/tokens'

/** Naive UI 主题覆盖全部来自 global.css 令牌，见 src/styles/tokens.ts */
const themeOverrides = buildThemeOverrides()
</script>

<template>
  <n-config-provider
    :theme="darkTheme"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-message-provider>
      <n-dialog-provider>
        <AppLayout>
          <router-view />
        </AppLayout>
        <GlobalConfirmDialog />
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
