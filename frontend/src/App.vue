<script setup lang="ts">
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme, zhCN, dateZhCN } from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import AppLayout from '@/components/AppLayout.vue'

/** 让 Naive UI 组件统一到墨青 + 青瓷的设计令牌上 */
const themeOverrides: GlobalThemeOverrides = {
  common: {
    fontFamily:
      "'HarmonyOS Sans SC', 'PingFang SC', 'Noto Sans SC', 'Source Han Sans SC', 'Microsoft YaHei', system-ui, sans-serif",
    fontSize: '14px',
    borderRadius: '3px',
    borderRadiusSmall: '2px',
    primaryColor: '#35b3a6',
    primaryColorHover: '#4fd1c2',
    primaryColorPressed: '#1f7a71',
    primaryColorSuppl: '#35b3a6',
    infoColor: '#35b3a6',
    infoColorHover: '#4fd1c2',
    successColor: '#57c98a',
    warningColor: '#d9a03c',
    errorColor: '#e04e39',
    bodyColor: '#0f1618',
    cardColor: '#151e21',
    modalColor: '#151e21',
    popoverColor: '#1d272b',
    tableColor: '#151e21',
    inputColor: '#0a1113',
    actionColor: '#1d272b',
    tableHeaderColor: '#151e21',
    borderColor: '#243035',
    dividerColor: '#243035',
    textColorBase: '#e6ecee',
    textColor1: '#e6ecee',
    textColor2: '#93a5ac',
    textColor3: '#5a6c74',
    placeholderColor: '#5a6c74',
    hoverColor: 'rgba(255, 255, 255, 0.035)'
  },
  Card: {
    borderColor: '#243035'
  },
  Button: {
    fontWeight: '500',
    fontWeightStrong: '600'
  },
  Menu: {
    itemTextColor: '#93a5ac'
  }
}
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
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>
