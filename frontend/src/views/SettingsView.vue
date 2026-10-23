<script setup lang="ts">
import { ref, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpace, NSpin, NInput, NInputNumber, NSelect, NSwitch,
  NTag, NPopconfirm, NDivider, useMessage,
} from 'naive-ui'
import {
  getSettings, updateSettings, testComfyui, testLlm,
  listChannels, updateChannels, testChannel,
  clearUploadCache, getHealth,
} from '@/api/settings'
import type { AppSettings, Channel } from '@/types/compose'

const message = useMessage()
const queryClient = useQueryClient()

// --- Tabs ---
const activeTab = ref('comfyui')
const tabs = [
  { key: 'comfyui', label: 'ComfyUI' },
  { key: 'llm', label: '语言模型' },
  { key: 'channels', label: '三方通道' },
  { key: 'workflows', label: '工作流' },
  { key: 'storage', label: '存储' },
  { key: 'about', label: '关于' },
]

// --- Settings data ---
const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: () => getSettings() })
const healthQuery = useQuery({ queryKey: ['health'], queryFn: () => getHealth(), refetchInterval: 10000 })

const editSettings = ref<Partial<AppSettings>>({})
watch(() => settingsQuery.data.value, (data) => {
  if (data) editSettings.value = { ...data }
}, { immediate: true })

const saveMutation = useMutation({
  mutationFn: (payload: Partial<AppSettings>) => updateSettings(payload),
  onSuccess: () => {
    message.success('设置已保存')
    queryClient.invalidateQueries({ queryKey: ['settings'] })
  },
  onError: (e: any) => message.error(`保存失败: ${e?.message ?? e}`),
})

// --- ComfyUI test ---
const testComfyuiMutation = useMutation({
  mutationFn: () => testComfyui(),
  onSuccess: () => message.success('ComfyUI 连接正常'),
  onError: (e: any) => message.error(`连接失败: ${e?.message ?? e}`),
})

// --- LLM test ---
const testLlmMutation = useMutation({
  mutationFn: () => testLlm(),
  onSuccess: () => message.success('语言模型连接正常'),
  onError: (e: any) => message.error(`连接失败: ${e?.message ?? e}`),
})

// --- Channels ---
const channelsQuery = useQuery({ queryKey: ['channels'], queryFn: () => listChannels() })
const editChannels = ref<Channel[]>([])
watch(() => channelsQuery.data.value, (data) => {
  if (data) editChannels.value = data
}, { immediate: true })

const saveChannelsMutation = useMutation({
  mutationFn: (channels: Channel[]) => updateChannels(channels),
  onSuccess: () => {
    message.success('通道配置已保存')
    queryClient.invalidateQueries({ queryKey: ['channels'] })
  },
  onError: (e: any) => message.error(`保存失败: ${e?.message ?? e}`),
})

const testChannelMutation = useMutation({
  mutationFn: (id: string) => testChannel(id),
  onSuccess: () => message.success('通道连接正常'),
  onError: (e: any) => message.error(`通道测试失败: ${e?.message ?? e}`),
})

// --- Clear cache ---
const clearCacheMutation = useMutation({
  mutationFn: () => clearUploadCache(),
  onSuccess: (data: any) => {
    if (data?.cached) {
      message.info('清理频率限制，请稍后再试')
    } else {
      message.success(`已清理 ${data?.removed_files ?? 0} 个文件，释放 ${(data?.freed_bytes ?? 0 / 1024 / 1024).toFixed(1)} MB`)
    }
  },
  onError: (e: any) => message.error(`清理失败: ${e?.message ?? e}`),
})

// --- LLM options ---
const llmProviderOptions = [
  { label: 'DeepSeek', value: 'deepseek' },
  { label: 'OpenAI', value: 'openai' },
  { label: '通义千问', value: 'qwen' },
  { label: 'Ollama', value: 'ollama' },
  { label: '自定义', value: 'custom' },
]
</script>

<template>
  <div class="settings-page">
    <div class="settings-layout">
      <!-- Sidebar nav -->
      <div class="settings-nav">
        <div
          v-for="tab in tabs"
          :key="tab.key"
          class="nav-item"
          :class="{ active: activeTab === tab.key }"
          @click="activeTab = tab.key"
        >{{ tab.label }}</div>
      </div>

      <!-- Content -->
      <div class="settings-content">
        <n-spin v-if="settingsQuery.isLoading.value" class="page-loading" />

        <!-- ComfyUI -->
        <n-card v-else-if="activeTab === 'comfyui'" title="ComfyUI 配置" class="settings-card">
          <n-space vertical :size="16">
            <div>
              <label class="field-label">ComfyUI 地址</label>
              <n-input v-model:value="editSettings.comfyui_base_url" placeholder="http://127.0.0.1:8188" />
            </div>
            <div>
              <label class="field-label">视频轮询超时（秒，最小 60）</label>
              <n-input-number v-model:value="editSettings.video_poll_timeout_seconds" :min="60" :default-value="300" />
            </div>
            <div>
              <label class="field-label">默认并发（固定为 1，只读）</label>
              <n-input-number :value="1" disabled />
            </div>
            <n-space>
              <n-button type="primary" :loading="saveMutation.isPending.value" @click="saveMutation.mutate(editSettings)">保存</n-button>
              <n-button :loading="testComfyuiMutation.isPending.value" @click="testComfyuiMutation.mutate()">测试连接</n-button>
            </n-space>
          </n-space>
        </n-card>

        <!-- LLM -->
        <n-card v-else-if="activeTab === 'llm'" title="语言模型配置" class="settings-card">
          <n-space vertical :size="16">
            <div>
              <label class="field-label">模型提供商</label>
              <n-select v-model:value="editSettings.llm_provider" :options="llmProviderOptions" />
            </div>
            <div>
              <label class="field-label">模型名称</label>
              <n-input v-model:value="editSettings.llm_model" placeholder="如 deepseek-chat" />
            </div>
            <div>
              <label class="field-label">API Key</label>
              <n-input v-model:value="editSettings.llm_api_key" type="password" show-password-on="click" :placeholder="editSettings.llm_api_key_set ? '已设置（留空清除）' : '请输入 API Key'" />
            </div>
            <n-space>
              <n-button type="primary" :loading="saveMutation.isPending.value" @click="saveMutation.mutate(editSettings)">保存</n-button>
              <n-button :loading="testLlmMutation.isPending.value" @click="testLlmMutation.mutate()">测试连接</n-button>
            </n-space>
          </n-space>
        </n-card>

        <!-- Channels -->
        <n-card v-else-if="activeTab === 'channels'" title="三方通道配置" class="settings-card">
          <n-space vertical :size="12">
            <div v-for="(ch, idx) in editChannels" :key="ch.id || idx" class="channel-row">
              <n-input v-model:value="ch.name" placeholder="通道名称" size="small" style="width: 120px" />
              <n-input v-model:value="ch.base_url" placeholder="Base URL" size="small" style="width: 200px" />
              <n-switch v-model:value="ch.enabled" size="small" />
              <n-input-number v-model:value="ch.priority" :min="1" size="small" style="width: 80px" />
              <n-button size="small" quaternary @click="testChannelMutation.mutate(ch.id)" :loading="testChannelMutation.isPending.value">测试</n-button>
            </div>
            <n-button type="primary" :loading="saveChannelsMutation.isPending.value" @click="saveChannelsMutation.mutate(editChannels)">保存通道配置</n-button>
          </n-space>
        </n-card>

        <!-- Workflows -->
        <n-card v-else-if="activeTab === 'workflows'" title="工作流管理" class="settings-card">
          <n-space vertical>
            <p class="hint-text">工作流管理请通过资产库页面上传与维护。被引用的工作流禁止删除。</p>
          </n-space>
        </n-card>

        <!-- Storage -->
        <n-card v-else-if="activeTab === 'storage'" title="存储管理" class="settings-card">
          <n-space vertical :size="16">
            <div>
              <label class="field-label">健康状态</label>
              <div class="health-grid">
                <div class="health-item">
                  <span>数据库</span>
                  <n-tag :type="healthQuery.data.value?.database ? 'success' : 'error'" size="small">{{ healthQuery.data.value?.database ? '正常' : '异常' }}</n-tag>
                </div>
                <div class="health-item">
                  <span>ComfyUI</span>
                  <n-tag :type="healthQuery.data.value?.comfyui ? 'success' : 'error'" size="small">{{ healthQuery.data.value?.comfyui ? '正常' : '异常' }}</n-tag>
                </div>
                <div class="health-item">
                  <span>Worker</span>
                  <n-tag :type="healthQuery.data.value?.worker ? 'success' : 'error'" size="small">{{ healthQuery.data.value?.worker ? '正常' : '异常' }}</n-tag>
                </div>
                <div class="health-item">
                  <span>磁盘</span>
                  <n-tag :type="healthQuery.data.value?.disk ? 'success' : 'error'" size="small">{{ healthQuery.data.value?.disk ? '正常' : '异常' }}</n-tag>
                </div>
              </div>
            </div>
            <n-divider />
            <div>
              <label class="field-label">清理上传缓存</label>
              <p class="hint-text">清理未使用的上传临时文件，释放磁盘空间。</p>
              <n-popconfirm @positive-click="clearCacheMutation.mutate()">
                <template #trigger>
                  <n-button type="warning" :loading="clearCacheMutation.isPending.value">清理缓存</n-button>
                </template>
                确认清理上传缓存？
              </n-popconfirm>
            </div>
          </n-space>
        </n-card>

        <!-- About -->
        <n-card v-else-if="activeTab === 'about'" title="关于" class="settings-card">
          <n-space vertical>
            <p>Director Lab 漫剧生产系统</p>
            <p class="hint-text">基于 ComfyUI 的 AI 漫剧生产工作台</p>
          </n-space>
        </n-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page { padding: 16px; min-height: calc(100vh - 56px); }
.settings-layout { display: flex; gap: 16px; }
.settings-nav { width: 180px; flex-shrink: 0; }
.nav-item {
  padding: 8px 12px; cursor: pointer; border-radius: 8px; font-size: 14px; color: #9CA3AF;
  transition: all 0.2s; margin-bottom: 4px;
}
.nav-item:hover { background: #202027; color: #E5E7EB; }
.nav-item.active { background: #6366F1; color: #fff; }
.settings-content { flex: 1; }
.settings-card { background: #18181D; border: 1px solid #26262E; border-radius: 12px; }
.field-label { display: block; font-size: 13px; color: #9CA3AF; margin-bottom: 4px; }
.hint-text { font-size: 13px; color: #9CA3AF; }
.page-loading { display: flex; justify-content: center; padding: 80px 0; }
.channel-row { display: flex; align-items: center; gap: 8px; }
.health-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.health-item { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; background: #202027; border-radius: 8px; font-size: 13px; color: #E5E7EB; }
</style>
