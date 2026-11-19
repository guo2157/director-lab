<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import WorkflowTestPanel from '@/components/workflow/WorkflowTestPanel.vue'
import {
  NCard, NButton, NSpin, NInput, NInputNumber, NSelect, NSwitch,
  NTag, NEmpty, NUpload, NTooltip, NModal, NPopconfirm, NIcon, useMessage,
} from 'naive-ui'
import { FilmOutline } from '@vicons/ionicons5'
import type { UploadFileInfo } from 'naive-ui'
import {
  getSettings, updateSettings, testLlm, listLlmModels,
  listComfyuiInstances, updateComfyuiInstances, testComfyui,
} from '@/api/settings'
import {
  listWorkflows, uploadWorkflow, validateWorkflow,
} from '@/api/workflows'
import type { LlmInstance, ComfyUIInstance, ComfyUIInstanceTestResult } from '@/types/compose'
import type { Workflow, WorkflowType } from '@/types/asset'

const message = useMessage()
const queryClient = useQueryClient()

// --- Tabs（带图标与描述） ---
const activeTab = ref('comfyui')
const tabs = [
  { key: 'comfyui', label: 'ComfyUI', desc: '多实例加权负载均衡' },
  { key: 'llm', label: '语言模型', desc: '剧本评估与解析模型' },
  { key: 'workflows', label: '工作流', desc: '工作流资产管理' },
  { key: 'about', label: '关于', desc: '系统信息' },
]

// --- Settings data ---
const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: () => getSettings() })

// --- LLM 多模型卡片配置（同一时刻仅一个启用，后端实时调用启用中的模型） ---
const editLlmInstances = ref<LlmInstance[]>([])
watch(() => settingsQuery.data.value, (data) => {
  if (data?.llm_instances) editLlmInstances.value = data.llm_instances.map((i) => ({ ...i }))
}, { immediate: true })

const saveLlmMutation = useMutation({
  mutationFn: () => updateSettings({ llm_instances: editLlmInstances.value }),
  onSuccess: () => {
    message.success('LLM 配置已保存')
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
  saveInstancesMutation.mutate(editInstances.value)
}

// 删除实例行（自动保存）
const removeInstance = (idx: number) => {
  editInstances.value.splice(idx, 1)
  saveInstancesMutation.mutate(editInstances.value)
}

// 行内编辑（权重/启用）自动保存（防抖 500ms，避免高频 API 调用）
let saveTimer: ReturnType<typeof setTimeout> | null = null
const handleInstanceChange = () => {
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    saveInstancesMutation.mutate(editInstances.value)
  }, 500)
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
const testingUrl = ref('')
const testInstanceMutation = useMutation({
  mutationFn: (baseUrl: string) => testComfyui(baseUrl),
  onMutate: (baseUrl: string) => { testingUrl.value = baseUrl },
  onSuccess: (data: any, baseUrl: string) => {
    const inst = editInstances.value.find((i) => i.base_url === baseUrl)
    instanceTestResults.value = {
      ...instanceTestResults.value,
      [baseUrl]: { base_url: baseUrl, enabled: inst?.enabled ?? true, weight: inst?.weight ?? 1, ok: data?.ok ?? false, latency_ms: data?.latency_ms, message: data?.message },
    }
    if (data?.ok) message.success(`连接正常（${data?.latency_ms ?? 0}ms）`)
    else message.error(`连接失败: ${data?.message || '未知错误'}`)
  },
  onError: (e: any) => message.error(`测试失败: ${e?.message ?? e}`),
  onSettled: () => { testingUrl.value = '' },
})

// --- LLM 逐卡片测试连接 ---
const testingLlmId = ref('')
const testLlmMutation = useMutation({
  mutationFn: (inst: LlmInstance) => {
    const payload: { base_url?: string; model?: string; api_key?: string } = {}
    if (inst.base_url) payload.base_url = inst.base_url
    if (inst.model) payload.model = inst.model
    if (inst.api_key && !String(inst.api_key).includes('*')) payload.api_key = inst.api_key
    return testLlm(Object.keys(payload).length ? payload : undefined)
  },
  onMutate: (inst: LlmInstance) => { testingLlmId.value = inst.id },
  onSuccess: (data: any) => message.success(`语言模型连接正常（${data?.latency_ms ?? 0}ms）`),
  onError: (e: any) => message.error(`连接失败: ${e?.message ?? e}`),
  onSettled: () => { testingLlmId.value = '' },
})

// --- LLM options ---
const llmProviderOptions = [
  { label: 'DeepSeek', value: 'deepseek' },
  { label: 'OpenAI', value: 'openai' },
  { label: 'Ollama', value: 'ollama' },
  { label: '自定义', value: 'custom' },
]
// 提供商 → 默认 API URL（切换时自动填充，用户仍可手动修改）
const LLM_PROVIDER_URLS: Record<string, string> = {
  deepseek: 'https://api.deepseek.com/v1',
  openai: 'https://api.openai.com/v1',
  ollama: 'http://127.0.0.1:11434/v1',
}
const handleLlmProviderChange = (inst: LlmInstance, value: string) => {
  inst.provider = value as LlmInstance['provider']
  const url = LLM_PROVIDER_URLS[value]
  if (url) inst.base_url = url
}
// 提供商值 → 显示名
const providerLabel = (value: string) => llmProviderOptions.find((o) => o.value === value)?.label || value || '未设置'
// 新增/编辑模型弹窗
const showLlmModal = ref(false)
const llmForm = ref<LlmInstance>({ id: '', name: '', provider: 'deepseek', base_url: LLM_PROVIDER_URLS.deepseek, api_key: '', model: '', enabled: false })
const openLlmModal = (inst?: LlmInstance) => {
  if (inst) {
    llmForm.value = { ...inst }
  } else {
    llmForm.value = {
      id: `llm-${Date.now()}`,
      name: '',
      provider: 'deepseek',
      base_url: LLM_PROVIDER_URLS.deepseek,
      api_key: '',
      model: '',
      enabled: editLlmInstances.value.length === 0,
    }
  }
  showLlmModal.value = true
}
// 弹窗确定：新增或回写编辑结果并保存
const confirmLlmModal = () => {
  const form = llmForm.value
  if (!form.base_url.trim()) {
    message.error('请输入模型 API URL')
    return
  }
  const idx = editLlmInstances.value.findIndex((i) => i.id === form.id)
  if (idx >= 0) {
    editLlmInstances.value[idx] = { ...form }
  } else {
    editLlmInstances.value.push({ ...form })
  }
  showLlmModal.value = false
  saveLlmMutation.mutate()
}
// 删除模型卡片（自动保存）
const removeLlmCard = (idx: number) => {
  editLlmInstances.value.splice(idx, 1)
  saveLlmMutation.mutate()
}
// 开启一个模型时自动关闭其余（同一时刻仅一个启用），切换后自动保存
const handleLlmEnable = (inst: LlmInstance, value: boolean) => {
  inst.enabled = value
  if (value) {
    for (const other of editLlmInstances.value) {
      if (other !== inst) other.enabled = false
    }
  }
  saveLlmMutation.mutate()
}
// --- LLM 逐卡片模型列表（查询下拉选择） ---
const llmModelOptionsMap = ref<Record<string, Array<{ label: string; value: string }>>>({})
const llmModelsLoadingMap = ref<Record<string, boolean>>({})
const fetchLlmModels = async (inst: LlmInstance) => {
  llmModelsLoadingMap.value = { ...llmModelsLoadingMap.value, [inst.id]: true }
  try {
    const payload: { base_url?: string; api_key?: string } = {}
    if (inst.base_url) payload.base_url = inst.base_url
    const key = inst.api_key
    if (key && !String(key).includes('*')) payload.api_key = String(key)
    const data = await listLlmModels(Object.keys(payload).length ? payload : undefined)
    llmModelOptionsMap.value = {
      ...llmModelOptionsMap.value,
      [inst.id]: (data.models ?? []).map((m: string) => ({ label: m, value: m })),
    }
    if (!(llmModelOptionsMap.value[inst.id] ?? []).length) message.warning('未查询到模型，可手动输入')
  } catch (e: any) {
    message.error(`查询模型列表失败: ${e?.message ?? e}`)
  } finally {
    llmModelsLoadingMap.value = { ...llmModelsLoadingMap.value, [inst.id]: false }
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
      <!-- Sidebar nav：用 button 保证键盘可聚焦与可访问名，激活态同时给出 aria-current -->
      <aside class="settings-nav">
        <div class="nav-header">设置</div>
        <button
          v-for="tab in tabs"
          :key="tab.key"
          type="button"
          class="nav-item"
          :class="{ active: activeTab === tab.key }"
          :aria-current="activeTab === tab.key ? 'page' : undefined"
          @click="activeTab = tab.key"
        >
          <span class="nav-text">
            <span class="nav-label">{{ tab.label }}</span>
            <span class="nav-desc">{{ tab.desc }}</span>
          </span>
        </button>
      </aside>

      <!-- Content -->
      <div class="settings-content">
        <!-- 首屏骨架：用内容占位，避免右侧空白 + 居中转圈造成的「卡死」观感 -->
        <div v-if="settingsQuery.isLoading.value" class="settings-skeleton" aria-hidden="true">
          <div class="sk-item sk-title" />
          <div class="sk-item sk-subtitle" />
          <div class="sk-card">
            <div v-for="i in 5" :key="i" class="sk-item sk-row" />
          </div>
        </div>

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
                <span class="col-actions">操作</span>
              </div>
              <div v-for="(inst, idx) in editInstances" :key="idx" class="wf-tr">
                <span class="col-name inst-url" data-label="实例地址">{{ inst.base_url || '（未填写）' }}</span>
                <span class="col-weight" data-label="权重"><n-input-number v-model:value="inst.weight" :min="1" size="small" style="width: 90px" @update:value="handleInstanceChange" /></span>
                <span class="col-enabled" data-label="启用"><n-switch v-model:value="inst.enabled" size="small" @update:value="handleInstanceChange" /></span>
                <span class="col-test" data-label="测试结果">
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
                <span class="col-actions" data-label="操作">
                  <n-button size="tiny" quaternary @click="testInstanceMutation.mutate(inst.base_url)" :loading="testingUrl === inst.base_url">测试</n-button>
                  <n-popconfirm @positive-click="removeInstance(idx)">
                    <template #trigger>
                      <n-button size="tiny" quaternary type="error">删除</n-button>
                    </template>
                    确认删除该实例？
                  </n-popconfirm>
                </span>
              </div>
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

        <!-- LLM（多模型卡片配置：同一时刻仅一个启用，后端实时调用启用中的模型） -->
        <template v-else-if="activeTab === 'llm'">
          <div class="page-header">
            <h2 class="page-title">语言模型配置</h2>
            <p class="page-subtitle">多模型卡片配置：开启一个模型后其余自动关闭，后端实时调用启用中的模型</p>
          </div>
          <div class="llm-card-toolbar">
            <n-tag size="small" :bordered="false" type="info">{{ editLlmInstances.length }} 个模型</n-tag>
            <n-button size="small" type="primary" @click="openLlmModal()">+ 新增模型</n-button>
          </div>
          <n-empty v-if="!editLlmInstances.length" description="暂无模型，点击右上角按钮新增" style="margin: 12px 0" />
          <div v-else class="llm-mini-grid">
            <div v-for="(inst, idx) in editLlmInstances" :key="inst.id" class="llm-mini-card" :class="{ active: inst.enabled }">
              <div class="llm-mini-head">
                <n-tag v-if="inst.enabled" type="success" size="small" :bordered="false">启用中</n-tag>
                <n-tag v-else size="small" :bordered="false">未启用</n-tag>
                <span class="llm-mini-name">{{ inst.name || inst.model || ('模型 ' + (idx + 1)) }}</span>
              </div>
              <div class="llm-mini-meta">
                <span class="llm-mini-provider">{{ providerLabel(inst.provider) }}</span>
                <span class="llm-mini-model hint-text">{{ inst.model || '未设置模型' }}</span>
              </div>
              <div class="llm-mini-url hint-text">{{ inst.base_url || '未设置 API URL' }}</div>
              <div class="llm-mini-ops">
                <n-switch :value="inst.enabled" size="small" @update:value="handleLlmEnable(inst, $event)" />
                <div class="llm-mini-btns">
                  <n-button size="tiny" quaternary @click="openLlmModal(inst)">编辑</n-button>
                  <n-popconfirm @positive-click="removeLlmCard(idx)">
                    <template #trigger>
                      <n-button size="tiny" quaternary type="error">删除</n-button>
                    </template>
                    确认删除该模型配置？
                  </n-popconfirm>
                </div>
              </div>
            </div>
          </div>

          <!-- 新增/编辑模型弹窗 -->
          <n-modal
            v-model:show="showLlmModal"
            preset="card"
            :title="llmForm.id ? '编辑模型' : '新增模型'"
            style="width: 560px; max-width: 92vw"
            :bordered="false"
          >
            <div class="form-grid">
              <div class="form-item">
                <label class="field-label">模型备注名</label>
                <n-input v-model:value="llmForm.name" placeholder="如 主力模型" />
              </div>
              <div class="form-item">
                <label class="field-label">模型提供商</label>
                <n-select :value="llmForm.provider" :options="llmProviderOptions" @update:value="handleLlmProviderChange(llmForm, $event)" />
              </div>
            </div>
            <div class="form-item">
              <label class="field-label">模型 API URL</label>
              <n-input v-model:value="llmForm.base_url" placeholder="如 https://api.deepseek.com/v1" />
            </div>
            <div class="form-item">
              <label class="field-label">API Key</label>
              <n-input v-model:value="llmForm.api_key" type="password" show-password-on="click" :placeholder="llmForm.api_key_set ? '已设置（留空清除）' : '请输入 API Key'" />
            </div>
            <div class="form-item">
              <label class="field-label">模型名称（点击查询按钮拉取列表，也可手动输入）</label>
              <div style="display: flex; gap: 8px; width: 100%">
                <n-select
                  v-model:value="llmForm.model"
                  :options="llmModelOptionsMap[llmForm.id] ?? []"
                  filterable
                  tag
                  clearable
                  :loading="llmModelsLoadingMap[llmForm.id]"
                  placeholder="查询模型列表后选择，或直接输入"
                  style="flex: 1"
                />
                <n-button :loading="llmModelsLoadingMap[llmForm.id]" @click="fetchLlmModels(llmForm)">查询模型</n-button>
              </div>
            </div>
            <div class="card-actions" style="margin-top: 0">
              <n-button :loading="testingLlmId === llmForm.id" @click="testLlmMutation.mutate(llmForm)">测试连接</n-button>
              <n-button type="primary" @click="confirmLlmModal">确定</n-button>
              <n-button quaternary @click="showLlmModal = false">取消</n-button>
            </div>
          </n-modal>
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
                <span class="col-name" data-label="名称">
                  <n-tooltip trigger="hover" placement="top-start" :disabled="!wf.description">
                    <template #trigger>
                      <span class="wf-name wf-name-hover">{{ wf.name }}</span>
                    </template>
                    <div class="wf-tooltip">{{ wf.description || '暂无描述' }}</div>
                  </n-tooltip>
                </span>
                <span class="col-type" data-label="类型">
                  <n-tag size="small" :bordered="false">{{ wf.workflow_type }}</n-tag>
                  <n-tag v-if="wf.is_builtin" type="warning" size="small" :bordered="false">内置</n-tag>
                  <n-tag v-if="wf.is_default" type="success" size="small" :bordered="false">默认</n-tag>
                </span>
                <span class="col-time hint-text" data-label="更新时间">{{ new Date(wf.updated_at).toLocaleString() }}</span>
                <span class="col-actions" data-label="操作">
                  <n-button size="tiny" quaternary @click="downloadWorkflow(wf)">下载</n-button>
                  <n-button size="tiny" quaternary @click="validateWorkflowMutation.mutate(wf.id)" :loading="validateWorkflowMutation.isPending.value">校验</n-button>
                </span>
              </div>
            </div>
          </n-card>

          <WorkflowTestPanel class="wf-test-panel" />

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
            <div class="about-brand"><n-icon :component="FilmOutline" :size="40" /></div>
            <h3 class="about-title">Director Lab 漫剧生产系统</h3>
            <div class="about-summary">
              <p class="summary-text">
                <strong>完全免费 · 本地化部署</strong>：所有算力来自自建 ComfyUI 实例，无需云端 API，零持续费用。支持多台闲置 GPU 机器组池，配合内网穿透即可扩展。
              </p>
              <p class="summary-text">
                <strong>三层负载均衡</strong>：实例健康熔断（心跳探测 + 自动摘除恢复）→ 实时负载调度（least-connections 按队列深度派发）→ 任务级故障转移（提交失败自动换实例重试），三重保障高可用。
              </p>
            </div>
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
  max-width: 1440px;
  margin: 0 auto;
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
  color: var(--text-secondary);
  padding: 8px 12px 10px;
  letter-spacing: 1px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background 0.18s ease, color 0.18s ease;
  color: var(--text-primary);
}
.nav-item:hover {
  background: var(--bg-elevated);
}
.nav-item:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
.nav-item.active {
  background: var(--brand-primary);
  color: var(--text-primary);
}
.nav-item.active .nav-desc {
  color: var(--overlay-active);
}
.nav-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.nav-label {
  font-size: 14px;
  font-weight: 500;
}
.nav-desc {
  display: block;
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
/* 设置页是二级页面：沿用全局衬线标题，字号收敛一级 */
.settings-content .page-title {
  font-size: 20px;
  margin: 0 0 4px;
}
.settings-content .page-subtitle {
  margin: 0;
}
.settings-card {
  border-radius: var(--radius-lg);
  box-shadow: none;
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
  grid-template-columns: minmax(200px, 1fr) 90px 70px 120px 120px;
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
.about-summary {
  margin-top: 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.summary-text {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin: 0;
}
.summary-text strong {
  color: var(--text-primary);
}
/* --- 首屏骨架 --- */
.settings-skeleton {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.sk-item {
  position: relative;
  overflow: hidden;
  background: var(--bg-panel);
  border-radius: var(--radius);
}
.sk-item::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    var(--overlay-hover) 45%,
    var(--overlay-active) 55%,
    transparent 100%
  );
  transform: translateX(-100%);
  animation: sk-slide 1.5s ease-in-out infinite;
}
.sk-title {
  width: 180px;
  height: 22px;
  margin-top: 4px;
}
.sk-subtitle {
  width: 340px;
  max-width: 100%;
  height: 13px;
  margin-bottom: var(--sp-3);
}
.sk-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-4);
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.sk-row {
  height: 44px;
  background: var(--bg-elevated);
}
@keyframes sk-slide {
  100% {
    transform: translateX(100%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .sk-item::after {
    animation: none;
    background: var(--overlay-hover);
  }
}
/* --- LLM 多模型小卡片列表布局 --- */
.llm-card-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.llm-mini-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.llm-mini-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 14px;
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  background: var(--bg-panel);
  transition: background 0.15s ease, border-color 0.15s ease;
}
.llm-mini-card:hover {
  background: var(--bg-elevated);
}
.llm-mini-card.active {
  border-color: var(--success-line);
}
.llm-mini-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.llm-mini-name {
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.llm-mini-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.llm-mini-provider {
  font-size: 12px;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.llm-mini-model {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.llm-mini-url {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.llm-mini-ops {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 2px;
}
.llm-mini-btns {
  display: flex;
  align-items: center;
  gap: 4px;
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

  /* 表格转卡片：两张表都是 CSS grid 固定列宽（实例表合计 600px、
     工作流表合计 620px），390px 下只能横向滚动，而移动端滚动条隐藏、
     右侧「测试/删除/下载/校验」等于不存在。
     改法：thead 隐藏，每行变成一张卡，列名由 data-label 的 ::before 提供 —— 
     不用 JS 重排，也不用改数据，纯 CSS 降级。 */
  .wf-thead {
    display: none;
  }
  .wf-table {
    gap: var(--sp-2);
  }
  .wf-tr {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    padding: var(--sp-3);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    background: var(--bg-elevated);
  }
  /* 卡片之间不要重复分隔线，边框已经表达了边界 */
  .wf-tr + .wf-tr {
    border-top: 1px solid var(--border-subtle);
  }
  /* 每个字段：标签在左、值在右，纵向堆叠。列名来自 data-label。 */
  .wf-tr > [data-label] {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-3);
    min-width: 0;
  }
  .wf-tr > [data-label]::before {
    content: attr(data-label);
    flex: none;
    font-size: 12px;
    color: var(--text-tertiary);
    letter-spacing: 0.02em;
  }
  /* 实例地址是长 URL，独占一行并允许换行，不参与「左标签右值」 */
  .wf-tr > .col-name[data-label] {
    display: block;
  }
  .wf-tr > .col-name[data-label]::before {
    display: block;
    margin-bottom: 4px;
  }
  .inst-table .wf-tr .inst-url {
    white-space: normal;
    word-break: break-all;
    font-size: 12.5px;
    color: var(--text-primary);
  }
  /* 操作按钮在窄屏下右对齐且不被压缩 */
  .wf-tr > .col-actions[data-label] {
    justify-content: flex-end;
    padding-top: 2px;
  }
}
</style>
