<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NCard, NTag, NSpace } from 'naive-ui'

const health = ref<string>('检查中...')
const healthType = ref<'default' | 'success' | 'error'>('default')

onMounted(async () => {
  try {
    const resp = await fetch('/api/health')
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    health.value = `后端联通正常：${data.app} v${data.version}`
    healthType.value = 'success'
  } catch (e) {
    health.value = `后端未联通：${e instanceof Error ? e.message : String(e)}`
    healthType.value = 'error'
  }
})
</script>

<template>
  <div style="max-width: 720px; margin: 48px auto; padding: 0 16px">
    <n-card title="Director Lab 漫剧生产工作台">
      <n-space vertical>
        <span>项目处于基础设施阶段（阶段 1）。项目管理、剧本、分镜等功能将在后续阶段实现。</span>
        <n-tag :type="healthType">{{ health }}</n-tag>
      </n-space>
    </n-card>
  </div>
</template>
