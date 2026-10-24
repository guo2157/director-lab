<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NCard, NButton, NSpin, NInput, NInputNumber, NSelect, NSwitch,
  NTag, NDivider, NEmpty, NUpload, NTooltip, NModal, useMessage,
} from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import {
  getSettings, updateSettings, testLlm, listLlmModels,
  listComfyuiInstances, updateComfyuiInstances, testComfyuiInstances,
} from '@/api/settings'
import {
  listWorkflows, uploadWorkflow, validateWorkflow,
} from '@/api/workflows'
import type { AppSettings, ComfyUIInstance, ComfyUIInstanceTestResult } from '@/types/compose'
import type { Workflow, WorkflowType } from '@/types/asset'

const message = useMessage()
const queryClient = useQueryClient()

// --- Tabs（带图标与描述） ---
const activeTab = ref('comfyui')
const tabs = [
  { key: 'comfyui', label: 'ComfyUI', icon: '🖥️', desc: '多实例加权负载均衡配置' },
  { key: 'llm', label: '语言模型', icon: '🤖', desc: '剧本评估与解析模型配置' },
  { key: 'workflows', label: '工作流', icon: '🧩', desc: '工作流资产管理说明' },
  { key: 'about', label: '关于', icon: 'ℹ️', desc: '系统信息' },
]

// --- Settings data ---
const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: () => getSettings() })

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

// --- ComfyUI 实例池（多实例加权负载均衡） ---
const instancesQuery = useQuery({ queryKey: ['comfyui-instances'], queryFn: () => listComfyuiInstances() })
const editInstances = ref<ComfyUIInstance[]>([])
watch(() => instancesQuery.data.value, (data) => {
  if (data) editInstances.value = data.map((i) => ({ ...i }))
}, { immediate: true })


// 新增实例弹窗
const showInstanceModal = ref(false)
const instanceForm = ref<ComfyUIInstance>({ base_url: '', weight: 1, enabled: true })
const openInstanceModal = () => {
  instanceForm.value = { base_url: '', weight: 1, enabled: true }
  showInstanceModal.value = true
}
const confirmAddInstance = () => {
  if (!instanceForm.value.base_url.trim()) {
    message.error('请输入实例地址')
    return
  }
  editInstances.value.push({ ...instanceForm.value, base_url: instanceForm.value.base_url.trim() })
  showInstanceModal.value = false
}

const saveInstancesMutation = useMutation({
  mutationFn: (instances: ComfyUIInstance[]) => updateComfyuiInstances(instances),
  onSuccess: () => {
    message.success('实例配置已保存')
    queryClient.invalidateQueries({ queryKey: ['comfyui-instances'] })
  },
  onError: (e: any) => message.error(`保存失败: ${e?.message ?? e}`),
})

// 逐实例连通性测试结果（base_url -> 结果）
const instanceTestResults = ref<Record<string, ComfyUIInstanceTestResult>>({})
const testInstancesMutation = useMutation({
  mutationFn: () => testComfyuiInstances(),
  onSuccess: (data: any) => {
    const map: Record<string, ComfyUIInstanceTestResult> = {}
    for (const r of data?.instances ?? []) map[r.base_url] = r
    instanceTestResults.value = map
    const okCount = (data?.instances ?? []).filter((r: any) => r.ok).length
    const total = (data?.instances ?? []).length
    if (okCount === total && total > 0) message.success(`全部 ${total} 个实例连接正常`)
    else message.warning(`${okCount}/${total} 个实例连接正常`)
  },
  onError: (e: any) => message.error(`测试失败: ${e?.message ?? e}`),
})

// --- LLM test ---
const testLlmMutation = useMutation({
  mutationFn: () => testLlm(),
  onSuccess: () => message.success('语言模型连接正常'),
  onError: (e: any) => message.error(`连接失败: ${e?.message ?? e}`),
})

// --- LLM options ---
const llmProviderOptions = [
  { label: 'DeepSeek', value: 'deepseek' },
  { label: 'OpenAI', value: 'openai' },
  { label: '通义千问', value: 'qwen' },
  { label: 'Ollama', value: 'ollama' },
  { label: '自定义', value: 'custom' },
]
// --- LLM 模型列表（查询下拉选择） ---
const llmModelOptions = ref<Array<{ label: string; value: string }>>([])
const llmModelsLoading = ref(false)
const fetchLlmModels = async () => {
  llmModelsLoading.value = true
  try {
    const payload: { base_url?: string; api_key?: string } = {}
    if (editSettings.value.llm_base_url) payload.base_url = String(editSettings.value.llm_base_url)
    const key = editSettings.value.llm_api_key
    if (key && !String(key).includes('*')) payload.api_key = String(key)
    const data = await listLlmModels(Object.keys(payload).length ? payload : undefined)
    llmModelOptions.value = (data.models ?? []).map((m: string) => ({ label: m, value: m }))
    if (!llmModelOptions.value.length) message.warning('未查询到模型，可手动输入')
  } catch (e: any) {
    message.error(`查询模型列表失败: ${e?.message ?? e}`)
  } finally {
    llmModelsLoading.value = false
  }
}

// --- 工作流管理 ---
const workflowTypeFilter = ref<WorkflowType | ''>('')
const workflowTypeOptions: Array<{ label: string; value: WorkflowType | '' }> = [
  { label: '全部', value: '' },
  { label: '图片', value: 'image' },
  { label: '视频', value: 'video' },
  { label: 'TTS', value: 'tts' },
  { label: 'BGM', value: 'bgm' },
  { label: '音效', value: 'sfx' },
]
const workflowsQuery = useQuery({
  queryKey: ['workflows', workflowTypeFilter],
  queryFn: () => listWorkflows(workflowTypeFilter.value ? { workflow_type: workflowTypeFilter.value } : undefined),
})
const workflowList = computed<Workflow[]>(() => {
  const data: any = workflowsQuery.data.value
  if (Array.isArray(data)) return data
  return data?.items ?? []
})

// 上传表单
const uploadForm = ref<{ name: string; workflow_type: WorkflowType; is_default: boolean; file: File | null }>({
  name: '',
  workflow_type: 'image',
  is_default: false,
  file: null,
})
const uploadTypeOptions = workflowTypeOptions.filter((o) => o.value !== '')

const uploadWorkflowMutation = useMutation({
  mutationFn: () => {
    if (!uploadForm.value.file) throw new Error('请选择工作流 JSON 文件')
    return uploadWorkflow(uploadForm.value.file, {
      name: uploadForm.value.name || uploadForm.value.file.name.replace(/\.json$/i, ''),
      workflow_type: uploadForm.value.workflow_type,
      is_default: uploadForm.value.is_default,
    })
  },
  onSuccess: () => {
    message.success('工作流已上传')
    showUploadModal.value = false
    uploadForm.value = { name: '', workflow_type: 'image', is_default: false, file: null }
    queryClient.invalidateQueries({ queryKey: ['workflows'] })
  },
  onError: (e: any) => message.error(`上传失败: ${e?.message ?? e}`),
})

// 新增工作流弹窗
const showUploadModal = ref(false)
const openUploadModal = () => {
  uploadForm.value = { name: '', workflow_type: 'image', is_default: false, file: null }
  showUploadModal.value = true
}

const onUploadFileChange = (options: { fileList: UploadFileInfo[] }) => {
  const file = options.fileList[0]?.file
  if (file && file instanceof File) {
    uploadForm.value.file = file
    if (!uploadForm.value.name) uploadForm.value.name = file.name.replace(/\.json$/i, '')
  } else {
    uploadForm.value.file = null
  }
}

const validateWorkflowMutation = useMutation({
  mutationFn: (id: string) => validateWorkflow(id),
  onSuccess: (data: any) => {
    if (data?.valid) message.success('工作流校验通过')
    else message.error(`校验失败: ${(data?.errors ?? []).join('; ')}`)
  },
  onError: (e: any) => message.error(`校验失败: ${e?.message ?? e}`),
})

// 下载工作流 JSON 文件
const downloadWorkflow = (wf: Workflow) => {
  if (!wf.definition) {
    message.error('该工作流无定义内容')
    return
  }
  const blob = new Blob([JSON.stringify(wf.definition, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${wf.name}.json`
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="settings-page">
    <div class="settings-layout">
      <!-- Sidebar nav -->
      <aside class="settings-nav">
        <div class="nav-header">设置</div>
        <div
          v-for="tab in tabs"
          :key="tab.key"
          class="nav-item"
          :class="{ active: activeTab === tab.key }"
          @click="activeTab = tab.key"
        >
          <span class="nav-icon">{{ tab.icon }}</span>
          <div class="nav-text">
            <div class="nav-label">{{ tab.label }}</div>
            <div class="nav-desc">{{ tab.desc }}</div>
          </div>
        </div>
      </aside>

      <!-- Content -->
      <div class="settings-content">
        <n-spin v-if="settingsQuery.isLoading.value" class="page-loading" />

        <!-- ComfyUI -->
        <template v-else-if="activeTab === 'comfyui'">
          <div class="page-header">
            <h2 class="page-title">ComfyUI 配置</h2>
            <p class="page-subtitle">多实例加权负载均衡：权重越大分配任务越多，实例不可用时自动切换</p>
          </div>
          <n-card class="settings-card" :bordered="false">
            <div class="section-label">
              <span>实例列表</span>
              <div style="display: flex; align-items: center; gap: 10px">
                <n-tag size="small" :bordered="false" type="info">{{ editInstances.length }} 个实例</n-tag>
                <n-button size="small" type="primary" @click="openInstanceModal">+ 新增实例</n-button>
              </div>
            </div>
            <n-empty v-if="!editInstances.length" description="暂无实例，点击右上角按钮新增" style="margin: 12px 0" />
            <div v-else class="wf-table inst-table">
              <div class="wf-thead">
                <span class="col-name">实例地址</span>
                <span class="col-weight">权重</span>
                <span class="col-enabled">启用</span>
                <span class="col-test">测试结果</span>
              </div>
              <div v-for="(inst, idx) in editInstances" :key="idx" class="wf-tr">
                <span class="col-name inst-url">{{ inst.base_url || '（未填写）' }}</span>
                <span class="col-weight"><n-input-number v-model:value="inst.weight" :min="1" size="small" style="width: 90px" /></span>
                <span class="col-enabled"><n-switch v-model:value="inst.enabled" size="small" /></span>
                <span class="col-test">
                  <n-tag
                    v-if="instanceTestResults[inst.base_url]"
                    :type="instanceTestResults[inst.base_url].ok ? 'success' : 'error'"
                    size="small"
                    :bordered="false"
                  >
                    {{ instanceTestResults[inst.base_url].ok ? `${instanceTestResults[inst.base_url].latency_ms ?? 0}ms` : (instanceTestResults[inst.base_url].message || '连接失败') }}
                  </n-tag>
                  <span v-else class="hint-text">未测试</span>
                </span>

              </div>
            </div>
            <n-divider />
            <div class="form-grid">
              <div class="form-item">
                <label class="field-label">视频轮询超时（秒）</label>
                <n-input-number v-model:value="editSettings.video_poll_timeout_seconds" :min="60" style="width: 100%" />
              </div>

            </div>
            <div class="card-actions">
              <n-button type="primary" :loading="saveInstancesMutation.isPending.value" @click="saveInstancesMutation.mutate(editInstances)">保存实例配置</n-button>
              <n-button :loading="testInstancesMutation.isPending.value" @click="testInstancesMutation.mutate()">测试全部实例</n-button>
            </div>
          </n-card>

          <!-- 新增实例弹窗 -->
          <n-modal
            v-model:show="showInstanceModal"
            preset="card"
            title="新增实例"
            style="width: 480px; max-width: 92vw"
            :bordered="false"
          >
            <div class="form-item">
              <label class="field-label">实例地址</label>
              <n-input v-model:value="instanceForm.base_url" placeholder="http://127.0.0.1:8188" />
            </div>
            <div class="form-item">
              <label class="field-label">权重（正整数，越大分配任务越多）</label>
              <n-input-number v-model:value="instanceForm.weight" :min="1" style="width: 100%" />
            </div>
            <div class="meta-item" style="margin: 12px 0 16px">
              <span>启用该实例</span>
              <n-switch v-model:value="instanceForm.enabled" size="small" />
            </div>
            <div class="card-actions" style="margin-top: 0">
              <n-button type="primary" @click="confirmAddInstance">添加</n-button>
              <n-button quaternary @click="showInstanceModal = false">取消</n-button>
            </div>
          </n-modal>
        </template>

        <!-- LLM -->
        <template v-else-if="activeTab === 'llm'">
          <div class="page-header">
            <h2 class="page-title">语言模型配置</h2>
            <p class="page-subtitle">用于剧本评估与解析，OpenAI 兼容接口</p>
          </div>
          <n-card class="settings-card" :bordered="false">
            <div class="form-grid">
              <div class="form-item">
                <label class="field-label">模型提供商</label>
                <n-select v-model:value="editSettings.llm_provider" :options="llmProviderOptions" />
              </div>
              <div class="form-item">
                <label class="field-label">模型 API URL</label>
                <n-input v-model:value="editSettings.llm_base_url" placeholder="如 https://api.deepseek.com/v1" />
              </div>
            </div>
            <div class="form-item">
              <label class="field-label">API Key</label>
              <n-input v-model:value="editSettings.llm_api_key" type="password" show-password-on="click" :placeholder="editSettings.llm_api_key_set ? '已设置（留空清除）' : '请输入 API Key'" />
            </div>
            <div class="form-item">
              <label class="field-label">模型名称（点击查询按钮拉取列表，也可手动输入）</label>
              <div style="display: flex; gap: 8px; width: 100%">
                <n-select
                  v-model:value="editSettings.llm_model"
                  :options="llmModelOptions"
                  filterable
                  tag
                  clearable
                  :loading="llmModelsLoading"
                  placeholder="查询模型列表后选择，或直接输入"
                  style="flex: 1"
                />
                <n-button :loading="llmModelsLoading" @click="fetchLlmModels">查询模型</n-button>
              </div>
            </div>
            <div class="card-actions">
              <n-button type="primary" :loading="saveMutation.isPending.value" @click="saveMutation.mutate(editSettings)">保存</n-button>
              <n-button :loading="testLlmMutation.isPending.value" @click="testLlmMutation.mutate()">测试连接</n-button>
            </div>
          </n-card>
        </template>

        <!-- Workflows -->
        <template v-else-if="activeTab === 'workflows'">
          <div class="page-header">
            <h2 class="page-title">工作流管理</h2>
            <p class="page-subtitle">悬停名称查看说明；内置工作流默认优先执行且不可删除</p>
          </div>

          <!-- 列表 -->
          <n-card class="settings-card" :bordered="false">
            <div class="wf-toolbar">
              <div class="wf-type-tabs">
                <div
                  v-for="opt in workflowTypeOptions"
                  :key="opt.value"
                  class="wf-type-tab"
                  :class="{ active: workflowTypeFilter === opt.value }"
                  @click="workflowTypeFilter = opt.value"
                >{{ opt.label }}</div>
              </div>
              <n-button size="small" type="primary" @click="openUploadModal">+ 新增工作流</n-button>
            </div>
            <n-spin v-if="workflowsQuery.isLoading.value" style="padding: 24px 0; width: 100%" />
            <n-empty v-else-if="!workflowList.length" description="暂无工作流" style="padding: 24px 0" />
            <div v-else class="wf-table">
              <div class="wf-thead">
                <span class="col-name">名称</span>
                <span class="col-type">类型</span>
                <span class="col-time">更新时间</span>
                <span class="col-actions">操作</span>
              </div>
              <div v-for="wf in workflowList" :key="wf.id" class="wf-tr">
                <span class="col-name">
                  <n-tooltip trigger="hover" placement="top-start" :disabled="!wf.description">
                    <template #trigger>
                      <span class="wf-name wf-name-hover">{{ wf.name }}</span>
                    </template>
                    <div class="wf-tooltip">{{ wf.description || '暂无描述' }}</div>
                  </n-tooltip>
                </span>
                <span class="col-type">
                  <n-tag size="small" :bordered="false">{{ wf.workflow_type }}</n-tag>
                  <n-tag v-if="wf.is_builtin" type="warning" size="small" :bordered="false">内置</n-tag>
                  <n-tag v-if="wf.is_default" type="success" size="small" :bordered="false">默认</n-tag>
                </span>
                <span class="col-time hint-text">{{ new Date(wf.updated_at).toLocaleString() }}</span>
                <span class="col-actions">
                  <n-button size="tiny" quaternary @click="downloadWorkflow(wf)">下载</n-button>
                  <n-button size="tiny" quaternary @click="validateWorkflowMutation.mutate(wf.id)" :loading="validateWorkflowMutation.isPending.value">校验</n-button>
                </span>
              </div>
            </div>
          </n-card>

          <!-- 新增工作流弹窗 -->
          <n-modal
            v-model:show="showUploadModal"
            preset="card"
            title="新增工作流"
            style="width: 480px; max-width: 92vw"
            :bordered="false"
          >
            <div class="form-item">
              <label class="field-label">名称</label>
              <n-input v-model:value="uploadForm.name" placeholder="工作流名称" />
            </div>
            <div class="form-item">
              <label class="field-label">类型</label>
              <n-select v-model:value="uploadForm.workflow_type" :options="uploadTypeOptions" />
            </div>
            <div class="form-item">
              <label class="field-label">工作流 JSON 文件（≤10MB）</label>
              <n-upload
                :max="1"
                accept=".json,application/json"
                :default-upload="false"
                @change="onUploadFileChange"
              >
                <n-button size="small">选择文件</n-button>
              </n-upload>
            </div>
            <div class="meta-item" style="margin: 12px 0 16px">
              <span>设为该类型默认</span>
              <n-switch v-model:value="uploadForm.is_default" size="small" />
            </div>
            <div class="card-actions" style="margin-top: 0">
              <n-button type="primary" :loading="uploadWorkflowMutation.isPending.value" @click="uploadWorkflowMutation.mutate()">上传</n-button>
              <n-button quaternary @click="showUploadModal = false">取消</n-button>
            </div>
          </n-modal>
        </template>

        <!-- About -->
        <template v-else-if="activeTab === 'about'">
          <div class="page-header">
            <h2 class="page-title">关于</h2>
            <p class="page-subtitle">系统信息</p>
          </div>
          <n-card class="settings-card" :bordered="false">
            <div class="about-brand">🎬</div>
            <h3 class="about-title">Director Lab 漫剧生产系统</h3>
            <p class="hint-text">基于 ComfyUI 的 AI 漫剧生产工作台</p>
          </n-card>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-page {
  height: 100%;
  overflow-y: auto;
  background: var(--bg-base);
}
.settings-layout {
  display: flex;
  gap: 20px;
  padding: 24px;
  align-items: stretch;
  min-height: 100%;
}

/* --- Sidebar --- */
.settings-nav {
  width: 216px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 10px;
  align-self: stretch;
}
.nav-header {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary, #6b7280);
  padding: 8px 12px 10px;
  letter-spacing: 1px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.18s ease, color 0.18s ease;
  color: var(--text-primary);
}
.nav-item:hover {
  background: var(--bg-elevated);
}
.nav-item.active {
  background: var(--brand-primary, #4f6ef7);
  color: #fff;
}
.nav-item.active .nav-desc {
  color: rgba(255, 255, 255, 0.75);
}
.nav-icon {
  font-size: 18px;
  line-height: 1;
}
.nav-text { min-width: 0; }
.nav-label {
  font-size: 14px;
  font-weight: 500;
}
.nav-desc {
  font-size: 11px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: color 0.18s ease;
}

/* --- Content --- */
.settings-content {
  flex: 1;
  min-width: 0;
}
.page-header {
  margin: 4px 0 16px;
}
.page-title {
  font-size: 20px;
  font-weight: 700;
  margin: 0 0 4px;
  color: var(--text-primary);
}
.page-subtitle {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0;
}
.settings-card {
  border-radius: 14px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

/* --- Section label --- */
.section-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 12px;
}

/* --- Instance table --- */
.inst-table .wf-thead,
.inst-table .wf-tr {
  grid-template-columns: minmax(200px, 1fr) 100px 80px 150px auto;
}
.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

/* --- Workflow list --- */
.wf-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.wf-type-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  border-bottom: 1px solid var(--border-subtle);
  flex: 1;
  min-width: 0;
  overflow-x: auto;
}
.wf-type-tab {
  padding: 8px 14px;
  font-size: 13px;
  color: var(--text-secondary);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.wf-type-tab:hover {
  color: var(--text-primary);
}
.wf-type-tab.active {
  color: var(--brand-primary);
  border-bottom-color: var(--brand-primary);
  font-weight: 600;
}
.wf-table {
  display: flex;
  flex-direction: column;
}
.wf-thead,
.wf-tr {
  display: grid;
  grid-template-columns: minmax(220px, 2fr) 220px 180px auto;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
}
.wf-thead {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  border-bottom: 1px solid var(--border-subtle);
}
.wf-tr {
  font-size: 13px;
  transition: background 0.15s ease;
}
.wf-tr:hover {
  background: var(--bg-elevated);
}
.wf-tr + .wf-tr {
  border-top: 1px solid var(--border-subtle);
}
.col-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.col-type {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: nowrap;
  overflow: hidden;
}
.col-tags {
  display: flex;
  align-items: center;
  gap: 4px;
}
.col-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  justify-content: flex-end;
}
.wf-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.wf-name-hover {
  cursor: help;
  text-decoration: underline dotted transparent;
  transition: text-decoration-color 0.15s ease;
}
.wf-name-hover:hover {
  text-decoration-color: var(--text-secondary);
}
.wf-tooltip {
  max-width: 420px;
  font-size: 12px;
  line-height: 1.6;
}

/* --- Forms --- */
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.form-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 4px;
}
.field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}
.card-actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
}

.inst-url {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-primary);
}

/* --- About --- */
.about-brand {
  font-size: 40px;
  text-align: center;
  margin-bottom: 8px;
}
.about-title {
  text-align: center;
  margin: 0 0 6px;
  font-size: 16px;
}

.hint-text {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0;
}
.page-loading {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}

/* --- Responsive --- */
@media (max-width: 768px) {
  .settings-layout {
    flex-direction: column;
    padding: 12px;
  }
  .settings-nav {
    width: 100%;
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
