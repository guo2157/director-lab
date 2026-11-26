<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import WorkflowTestPanel from '@/components/workflow/WorkflowTestPanel.vue'
import {
  NCard, NButton, NInput, NInputNumber, NSelect, NSwitch,
  NTag, NUpload, NTooltip, NModal, NIcon, useMessage,
} from 'naive-ui'
import {
  AddOutline, FilmOutline, LayersOutline, ServerOutline, SparklesOutline,
} from '@vicons/ionicons5'
import type { UploadFileInfo } from 'naive-ui'
import {
  getSettings, updateSettings, testLlm, listLlmModels,
  listComfyuiInstances, updateComfyuiInstances, testComfyui,
} from '@/api/settings'
import {
  getWorkflow, getWorkflowClassification, getWorkflowModelTypes, updateWorkflow,
  uploadWorkflow, validateWorkflow,
} from '@/api/workflows'
import type { LlmInstance, ComfyUIInstance, ComfyUIInstanceTestResult } from '@/types/compose'
import type { Workflow, WorkflowType } from '@/types/asset'
import type { WorkflowModelType } from '@/types/workflow'
import {
  MODE_LABELS,
  type WorkflowClassifiedItem,
  type WorkflowClassificationGroup,
} from '@/types/workflow'
import EmptyState from '@/components/ui/EmptyState.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const message = useMessage()
const queryClient = useQueryClient()
const confirmDialog = useConfirmDialog()

// --- Tabs（带图标与描述） ---
const route = useRoute()
const TAB_KEYS = ['comfyui', 'llm', 'workflows', 'about'] as const
type TabKey = (typeof TAB_KEYS)[number]
/** 支持 ?tab=xxx 直达（顶栏「尚未配置 ComfyUI」确认后落到 tab=comfyui）。
 *  只接受已知 key，非法值回落到默认档，不让 URL 决定渲染出不存在的面板。 */
function resolveTab(value: unknown): TabKey {
  const raw = Array.isArray(value) ? value[0] : value
  return TAB_KEYS.includes(raw as TabKey) ? (raw as TabKey) : 'comfyui'
}
const activeTab = ref<TabKey>(resolveTab(route.query.tab))
// 同页参数变化（不含组件重挂载）时同步标签页
watch(
  () => route.query.tab,
  (v) => {
    activeTab.value = resolveTab(v)
  }
)
const tabs: { key: TabKey; label: string; desc: string }[] = [
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

// 删除实例行（确认后自动保存）
const removeInstance = (idx: number) => {
  const inst = editInstances.value[idx]
  const label = inst?.base_url || '未填写地址的实例'
  confirmDialog({
    title: '删除实例',
    message: `删除后「${label}」不再参与任务分配，已提交的任务不受影响。`,
    impact: [`实例「${label}」`, ...(inst ? [`权重 ${inst.weight}`] : [])],
    confirmText: '删除实例',
    onConfirm: () => {
      editInstances.value.splice(idx, 1)
      saveInstancesMutation.mutate(editInstances.value)
    },
  })
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
// 弹窗标题按「是否已在列表中」判断，不能看 id：新增时也会先生成一个本地 id
const llmEditing = computed(() => editLlmInstances.value.some((i) => i.id === llmForm.value.id))
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
// 删除模型卡片（确认后自动保存）
const removeLlmCard = (idx: number) => {
  const inst = editLlmInstances.value[idx]
  const label = inst?.name || inst?.model || `模型 ${idx + 1}`
  confirmDialog({
    title: '删除模型配置',
    message: `删除后「${label}」不再出现在模型列表中；若它正在启用，删除后需另选一个模型。`,
    impact: [`模型「${label}」`, ...(inst?.base_url ? [`API ${inst.base_url}`] : [])],
    confirmText: '删除模型',
    onConfirm: () => {
      editLlmInstances.value.splice(idx, 1)
      saveLlmMutation.mutate()
    },
  })
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
]
/** BGM / 音效不再在设置页展示：音频由视频工作流自带，配音音色在角色页处理 */
const HIDDEN_WORKFLOW_TYPES = new Set<string>(['bgm', 'sfx'])
/**
 * 工作流细分分类：分组由后端按 capabilities.normalize() 的真实能力算好
 * （文生图 / 图生图 / 首尾帧 / 文生视频 / 参考图生视频 / TTS / BGM / SFX）。
 * 前端只按 workflow_type 做一级筛选，不按名称或描述二次归类。
 */
const classificationQuery = useQuery({
  queryKey: ['workflows', 'classification'],
  queryFn: () => getWorkflowClassification(),
})
const classificationGroups = computed<WorkflowClassificationGroup[]>(() => {
  const groups = (classificationQuery.data.value?.groups ?? [])
    .filter((g) => !HIDDEN_WORKFLOW_TYPES.has(g.workflow_type))
  if (!workflowTypeFilter.value) return groups
  return groups.filter((g) => g.workflow_type === workflowTypeFilter.value)
})
/** 能力判不出细分类型的工作流：单独成组展示，不硬塞进任何分类 */
const uncategorizedItems = computed<WorkflowClassifiedItem[]>(() => {
  const items = (classificationQuery.data.value?.uncategorized ?? [])
    .filter((i) => !HIDDEN_WORKFLOW_TYPES.has(i.workflow_type))
  if (!workflowTypeFilter.value) return items
  return items.filter((i) => i.workflow_type === workflowTypeFilter.value)
})
const classifiedCount = computed(
  () =>
    classificationGroups.value.reduce((n, g) => n + g.items.length, 0)
    + uncategorizedItems.value.length,
)
/** 分组里的能力标签：用统一中文文案，未知模式直接显示原始键名 */
function modeLabel(mode: string): string {
  return MODE_LABELS[mode] ?? mode
}

/** 渲染用分组：正常分类 +（有内容时）待检查组，模板只需一套行结构 */
interface RenderGroup {
  key: string
  label: string
  items: WorkflowClassifiedItem[]
}
const renderGroups = computed<RenderGroup[]>(() => {
  const list: RenderGroup[] = classificationGroups.value
    .filter((g) => g.items.length)
    .map((g) => ({ key: g.key, label: g.label, items: g.items }))
  if (uncategorizedItems.value.length) {
    list.push({ key: '__uncategorized__', label: '待检查', items: uncategorizedItems.value })
  }
  return list
})

// 上传表单
const uploadForm = ref<{
  name: string
  workflow_type: WorkflowType
  /** 模型类型：图片的文生图/图生图、视频的参考图生视频、TTS 语音合成 */
  model_type: WorkflowModelType | null
  is_default: boolean
  file: File | null
}>({
  name: '',
  workflow_type: 'image',
  model_type: null,
  is_default: false,
  file: null,
})
const uploadTypeOptions = workflowTypeOptions.filter((o) => o.value !== '')

/**
 * 模型类型选项：由后端 /workflows/model-types 给出，按类型联动展示。
 * 图片：文生图 / 图生图；视频：参考图生视频（可选）+ 首尾帧、文生视频（预留置灰）；
 * TTS：语音合成。前端不自行猜能力，只按后端 enabled 标记决定能否提交。
 */
const modelTypesQuery = useQuery({
  queryKey: ['workflows', 'model-types'],
  queryFn: () => getWorkflowModelTypes(),
  staleTime: 5 * 60_000,
})

const uploadModelTypeOptions = computed(() => {
  const group = (modelTypesQuery.data.value?.items ?? [])
    .find((g) => g.workflow_type === uploadForm.value.workflow_type)
  return (group?.model_types ?? []).map((o) => ({
    label: o.enabled ? o.label : `${o.label}（暂未开放）`,
    value: o.value,
    disabled: !o.enabled,
  }))
})

/** 当前选择是否为一个已开放的模型类型（决定上传按钮能否点击） */
const uploadModelTypeValid = computed(() => {
  if (!uploadForm.value.model_type) return false
  const group = (modelTypesQuery.data.value?.items ?? [])
    .find((g) => g.workflow_type === uploadForm.value.workflow_type)
  const hit = (group?.model_types ?? []).find((o) => o.value === uploadForm.value.model_type)
  return Boolean(hit?.enabled)
})

/** 切换类型时清空模型类型，等用户在同一类型下重新选择 */
watch(
  () => uploadForm.value.workflow_type,
  () => {
    uploadForm.value.model_type = null
  },
)

const uploadWorkflowMutation = useMutation({
  mutationFn: () => {
    if (!uploadForm.value.file) throw new Error('请选择工作流 JSON 文件')
    return uploadWorkflow(uploadForm.value.file, {
      name: uploadForm.value.name || uploadForm.value.file.name.replace(/\.json$/i, ''),
      workflow_type: uploadForm.value.workflow_type,
      model_type: uploadForm.value.model_type,
      is_default: uploadForm.value.is_default,
    })
  },
  onSuccess: () => {
    message.success('工作流已上传')
    showUploadModal.value = false
    uploadForm.value = {
      name: '', workflow_type: 'image', model_type: null, is_default: false, file: null,
    }
    queryClient.invalidateQueries({ queryKey: ['workflows'] })
  },
  onError: (e: any) => message.error(`上传失败: ${e?.message ?? e}`),
})

// 新增工作流弹窗
const showUploadModal = ref(false)
const openUploadModal = () => {
  uploadForm.value = {
    name: '', workflow_type: 'image', model_type: null, is_default: false, file: null,
  }
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

// 下载工作流 JSON 文件：分类接口只返回摘要，下载前按 id 取回完整定义
const downloadWorkflow = async (wf: { id: string; name: string }) => {
  try {
    const full: Workflow = await getWorkflow(wf.id)
    if (!full.definition) {
      message.error('该工作流无定义内容')
      return
    }
    const blob = new Blob([JSON.stringify(full.definition, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${full.name}.json`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    message.error(`下载失败: ${e?.message ?? e}`)
  }
}

// --- 描述编辑：描述是资产库 / 分镜图 / 分镜看板选择器的主展示文本，必须可维护 ---
const showDescModal = ref(false)
const descWorkflow = ref<WorkflowClassifiedItem | null>(null)
const descValue = ref('')
function openDescEdit(wf: WorkflowClassifiedItem) {
  descWorkflow.value = wf
  descValue.value = wf.description
  showDescModal.value = true
}
/** 与后端同口径：不超过 20 个汉字或 40 个英文（ASCII）字符 */
const descCharCount = computed(() => {
  const chars = [...descValue.value.trim()]
  const ascii = chars.filter((c) => c.charCodeAt(0) < 128).length
  return { total: chars.length, ascii, nonAscii: chars.length - ascii }
})
const descTooLong = computed(
  () => descCharCount.value.nonAscii > 20 || descCharCount.value.ascii > 40,
)
const saveDescMutation = useMutation({
  mutationFn: () => {
    const wf = descWorkflow.value
    if (!wf) throw new Error('未选择工作流')
    return updateWorkflow(wf.id, { description: descValue.value.trim() })
  },
  onSuccess: () => {
    message.success('描述已更新，各页面选择器的显示文本同步变更')
    showDescModal.value = false
    queryClient.invalidateQueries({ queryKey: ['workflows'] })
    queryClient.invalidateQueries({ queryKey: ['workflow-candidates'] })
    queryClient.invalidateQueries({ queryKey: ['project-workflow-config'] })
  },
  onError: (e: any) => message.error(`保存失败: ${e?.message ?? e}`),
})
</script>

<template>
  <div class="settings-page">
    <!-- 顶栏已高亮「设置」：页内不再重复同名可视标题，H1 只保留语义 -->
    <h1 class="visually-hidden">设置</h1>
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
          <!-- 左侧导航已用「品牌底 + 品牌字 + 内描边」标出当前档，页内不再重复同名标题 -->
          <div class="tab-head">
            <h2 class="visually-hidden">ComfyUI 配置</h2>
            <p class="page-subtitle">多实例加权负载均衡：权重越大分配任务越多，实例不可用时自动切换</p>
          </div>
          <n-card class="settings-card" :bordered="false">
            <div class="section-label">
              <span>实例列表</span>
              <div class="section-tools">
                <n-tag size="small" :bordered="false" type="info">{{ editInstances.length }} 个实例</n-tag>
                <n-button size="small" type="primary" @click="openInstanceModal">
                  <template #icon><n-icon :component="AddOutline" /></template>
                  新增实例
                </n-button>
              </div>
            </div>
            <EmptyState
              v-if="!editInstances.length"
              compact
              class="in-card-empty"
              title="还没有 ComfyUI 实例"
              description="添加实例地址后，生成任务会按权重分配到各实例。"
              action-text="新增实例"
              @action="openInstanceModal"
            >
              <template #icon><n-icon :component="ServerOutline" :size="22" /></template>
            </EmptyState>
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
                <span class="col-weight" data-label="权重"><n-input-number v-model:value="inst.weight" :min="1" size="small" class="weight-input" :aria-label="`实例权重 ${inst.base_url || idx + 1}`" @update:value="handleInstanceChange" /></span>
                <span class="col-enabled" data-label="启用"><n-switch v-model:value="inst.enabled" size="small" :aria-label="`启用实例 ${inst.base_url || idx + 1}`" @update:value="handleInstanceChange" /></span>
                <span class="col-test" data-label="测试结果">
                  <n-tag
                    v-if="instanceTestResults[inst.base_url]"
                    :type="instanceTestResults[inst.base_url].ok ? 'success' : 'error'"
                    size="small"
                    :bordered="false"
                  >
                    {{ instanceTestResults[inst.base_url].ok
                      ? `连接正常 ${instanceTestResults[inst.base_url].latency_ms ?? 0}ms`
                      : (instanceTestResults[inst.base_url].message || '连接失败') }}
                  </n-tag>
                  <span v-else class="hint-text">未测试</span>
                </span>
                <span class="col-actions" data-label="操作">
                  <n-button size="tiny" quaternary @click="testInstanceMutation.mutate(inst.base_url)" :loading="testingUrl === inst.base_url">测试</n-button>
                  <n-button size="tiny" quaternary type="error" @click="removeInstance(idx)">删除</n-button>
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
              <n-input-number v-model:value="instanceForm.weight" :min="1" class="field-full" />
            </div>
            <div class="switch-row">
              <span>启用该实例</span>
              <n-switch v-model:value="instanceForm.enabled" size="small" aria-label="启用该实例" />
            </div>
            <div class="modal-actions">
              <span class="spacer" />
              <n-button quaternary @click="showInstanceModal = false">取消</n-button>
              <n-button type="primary" @click="confirmAddInstance">添加实例</n-button>
            </div>
          </n-modal>
        </template>

        <!-- LLM（多模型卡片配置：同一时刻仅一个启用，后端实时调用启用中的模型） -->
        <template v-else-if="activeTab === 'llm'">
          <div class="tab-head">
            <h2 class="visually-hidden">语言模型配置</h2>
            <p class="page-subtitle">多模型卡片配置：开启一个模型后其余自动关闭，后端实时调用启用中的模型</p>
          </div>
          <div class="llm-card-toolbar">
            <n-tag size="small" :bordered="false" type="info">{{ editLlmInstances.length }} 个模型</n-tag>
            <n-button size="small" type="primary" @click="openLlmModal()">
              <template #icon><n-icon :component="AddOutline" /></template>
              新增模型
            </n-button>
          </div>
          <EmptyState
            v-if="!editLlmInstances.length"
            compact
            class="in-card-empty"
            title="还没有语言模型"
            description="添加模型地址与 API Key 后，剧本评估与解析会调用启用中的模型。"
            action-text="新增模型"
            @action="openLlmModal()"
          >
            <template #icon><n-icon :component="SparklesOutline" :size="22" /></template>
          </EmptyState>
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
                <n-switch
                  :value="inst.enabled"
                  size="small"
                  :aria-label="`启用模型 ${inst.name || inst.model || idx + 1}`"
                  @update:value="handleLlmEnable(inst, $event)"
                />
                <div class="llm-mini-btns">
                  <n-button size="tiny" quaternary @click="openLlmModal(inst)">编辑</n-button>
                  <n-button size="tiny" quaternary type="error" @click="removeLlmCard(idx)">删除</n-button>
                </div>
              </div>
            </div>
          </div>

          <!-- 新增/编辑模型弹窗 -->
          <n-modal
            v-model:show="showLlmModal"
            preset="card"
            :title="llmEditing ? '编辑模型' : '新增模型'"
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
              <div class="field-row">
                <n-select
                  v-model:value="llmForm.model"
                  :options="llmModelOptionsMap[llmForm.id] ?? []"
                  filterable
                  tag
                  clearable
                  :loading="llmModelsLoadingMap[llmForm.id]"
                  placeholder="查询模型列表后选择，或直接输入"
                  class="field-grow"
                />
                <n-button :loading="llmModelsLoadingMap[llmForm.id]" @click="fetchLlmModels(llmForm)">查询模型</n-button>
              </div>
            </div>
            <div class="modal-actions">
              <n-button :loading="testingLlmId === llmForm.id" @click="testLlmMutation.mutate(llmForm)">测试连接</n-button>
              <span class="spacer" />
              <n-button quaternary @click="showLlmModal = false">取消</n-button>
              <n-button type="primary" @click="confirmLlmModal">保存模型</n-button>
            </div>
          </n-modal>
        </template>

        <!-- Workflows -->
        <template v-else-if="activeTab === 'workflows'">
          <div class="tab-head">
            <h2 class="visually-hidden">工作流管理</h2>
            <p class="page-subtitle">
              按真实能力自动细分（文生图 / 图生图 / 首尾帧 / 文生视频 / 参考图生视频 / TTS / BGM / SFX），
              共 {{ classifiedCount }} 个；描述可在行内编辑，内置工作流默认优先执行且不可删除
            </p>
          </div>

          <!-- 列表 -->
          <n-card class="settings-card" :bordered="false">
            <div class="wf-toolbar">
              <div class="wf-type-tabs" role="group" aria-label="按工作流类型筛选">
                <button
                  v-for="opt in workflowTypeOptions"
                  :key="opt.value"
                  type="button"
                  class="wf-type-tab"
                  :class="{ active: workflowTypeFilter === opt.value }"
                  :aria-pressed="workflowTypeFilter === opt.value"
                  @click="workflowTypeFilter = opt.value"
                >{{ opt.label }}</button>
              </div>
              <n-button size="small" type="primary" @click="openUploadModal">
                <template #icon><n-icon :component="AddOutline" /></template>
                新增工作流
              </n-button>
            </div>
            <div v-if="classificationQuery.isLoading.value" class="wf-skeleton" aria-hidden="true">
              <div v-for="i in 5" :key="i" class="sk-item sk-row" />
            </div>
            <EmptyState
              v-else-if="classificationQuery.isError.value"
              compact
              class="in-card-empty"
              title="工作流分类加载失败"
              description="无法获取工作流列表，请确认后端服务可用后重试。"
              action-text="重新加载"
              @action="classificationQuery.refetch()"
            >
              <template #icon><n-icon :component="LayersOutline" :size="22" /></template>
            </EmptyState>
            <EmptyState
              v-else-if="!renderGroups.length"
              compact
              class="in-card-empty"
              title="还没有工作流"
              description="上传 ComfyUI 导出的工作流 JSON，即可在生成任务中选用。"
              action-text="新增工作流"
              @action="openUploadModal"
            >
              <template #icon><n-icon :component="LayersOutline" :size="22" /></template>
            </EmptyState>
            <div v-else class="wf-groups">
              <section v-for="group in renderGroups" :key="group.key" class="wf-group">
                <div class="wf-group-head">
                  <span class="wf-group-title">{{ group.label }}</span>
                  <span class="wf-group-count">{{ group.items.length }}</span>
                  <span v-if="group.key === '__uncategorized__'" class="wf-group-hint">
                    能力声明不足以判定细分类型，未归入任何分类；请检查该工作流的能力标记
                  </span>
                </div>
                <div class="wf-table">
                  <div v-for="wf in group.items" :key="wf.id" class="wf-tr">
                    <span class="col-name" data-label="描述">
                      <n-tooltip trigger="hover" placement="top-start" :disabled="!wf.name">
                        <template #trigger>
                          <span class="wf-name wf-name-hover">{{ wf.description }}</span>
                        </template>
                        <div class="wf-tooltip">技术名：{{ wf.name }}</div>
                      </n-tooltip>
                      <span class="wf-sub">v{{ wf.version }} · {{ wf.name }}</span>
                    </span>
                    <span class="col-type" data-label="能力">
                      <n-tag
                        v-for="mode in wf.generation_modes.slice(0, 3)"
                        :key="mode"
                        size="small"
                        :bordered="false"
                      >{{ modeLabel(mode) }}</n-tag>
                      <span v-if="!wf.generation_modes.length" class="hint-text">未声明能力</span>
                    </span>
                    <span class="col-time col-state" data-label="状态">
                      <n-tag v-if="wf.is_default" type="success" size="small" :bordered="false">默认</n-tag>
                      <n-tag v-if="wf.is_builtin" type="warning" size="small" :bordered="false">内置</n-tag>
                      <span v-if="!wf.is_default && !wf.is_builtin" class="hint-text">自定义</span>
                    </span>
                    <span class="col-actions" data-label="操作">
                      <n-button size="tiny" quaternary @click="openDescEdit(wf)">编辑描述</n-button>
                      <n-button size="tiny" quaternary @click="downloadWorkflow(wf)">下载</n-button>
                      <n-button size="tiny" quaternary @click="validateWorkflowMutation.mutate(wf.id)" :loading="validateWorkflowMutation.isPending.value">校验</n-button>
                    </span>
                  </div>
                </div>
              </section>
            </div>
          </n-card>

          <WorkflowTestPanel class="wf-test-panel" />
          <!-- 描述编辑弹窗：描述是各页面选择器的主展示文本，改这里全站同步 -->
          <n-modal
            v-model:show="showDescModal"
            preset="card"
            title="编辑工作流描述"
            style="width: 460px; max-width: 92vw"
            :bordered="false"
          >
            <p class="hint-text modal-hint">
              描述显示在资产库、分镜图、分镜看板的工作流选择器中，技术名只在悬停时提示。
            </p>
            <div class="form-item">
              <label class="field-label">描述</label>
              <n-input
                v-model:value="descValue"
                placeholder="如：千问文生图"
                maxlength="60"
                show-count
              />
              <span class="hint-text" :class="{ 'is-danger': descTooLong }">
                当前 {{ descCharCount.nonAscii }} 个汉字 / {{ descCharCount.ascii }} 个英文字符，上限 20 个汉字或 40 个英文字符
              </span>
            </div>
            <p v-if="descWorkflow" class="hint-text modal-hint-sub">
              技术名：{{ descWorkflow.name }}（v{{ descWorkflow.version }}）
            </p>
            <div class="modal-actions">
              <span class="spacer" />
              <n-button quaternary @click="showDescModal = false">取消</n-button>
              <n-button
                type="primary"
                :disabled="!descValue.trim() || descTooLong"
                :loading="saveDescMutation.isPending.value"
                @click="saveDescMutation.mutate()"
              >保存描述</n-button>
            </div>
          </n-modal>

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
              <label class="field-label">模型类型</label>
              <n-select
                v-model:value="uploadForm.model_type"
                :options="uploadModelTypeOptions"
                :loading="modelTypesQuery.isLoading.value"
                placeholder="选择模型类型"
              />
              <div v-if="uploadForm.workflow_type === 'video'" class="field-hint">
                视频目前仅支持参考图生视频，首尾帧与文生视频为预留能力
              </div>
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
            <div class="switch-row">
              <span>设为该类型默认</span>
              <n-switch v-model:value="uploadForm.is_default" size="small" aria-label="设为该类型默认" />
            </div>
            <div class="modal-actions">
              <span class="spacer" />
              <n-button quaternary @click="showUploadModal = false">取消</n-button>
              <n-button
                type="primary"
                :loading="uploadWorkflowMutation.isPending.value"
                :disabled="!uploadModelTypeValid"
                @click="uploadWorkflowMutation.mutate()"
              >上传工作流</n-button>
            </div>
          </n-modal>

        </template>

        <!-- About -->
        <template v-else-if="activeTab === 'about'">
          <h2 class="visually-hidden">关于</h2>
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
  gap: var(--sp-5);
  padding: var(--sp-6);
  align-items: stretch;
  min-height: 100%;
  max-width: var(--page-max);
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
  border-radius: var(--radius-lg);
  padding: var(--sp-3);
  align-self: stretch;
}
.nav-header {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-tertiary);
  padding: var(--sp-1) var(--sp-3) var(--sp-2);
  letter-spacing: 0.08em;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  width: 100%;
  padding: var(--sp-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast), color var(--transition-fast);
  color: var(--text-primary);
}
.nav-item:hover {
  background: var(--bg-elevated);
}
.nav-item:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
/* 当前档三重编码：品牌 soft 底 + 品牌字 + 内描边，另加 600 字重。
   不用品牌实色填充 —— 青底配 --text-primary 只有 1.9:1，是 DESIGN 明令禁止的组合。 */
.nav-item.active {
  background: var(--brand-soft);
  border-color: var(--brand-line);
  color: var(--brand-hover);
}
.nav-item.active .nav-label {
  font-weight: 600;
}
.nav-item.active .nav-desc {
  color: var(--text-secondary);
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
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: color var(--transition-fast);
}

/* --- Content --- */
.settings-content {
  flex: 1;
  min-width: 0;
}
/* 页内不重复左侧导航已高亮的档名，只保留一行说明（H2 见 .visually-hidden） */
.tab-head {
  margin: 0 0 var(--sp-4);
}
.tab-head .page-subtitle {
  margin: 0;
  max-width: 72ch;
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
  gap: var(--sp-3);
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: var(--sp-3);
}
.section-tools {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
}

/* --- Instance table --- */
.inst-table .wf-thead,
.inst-table .wf-tr {
  grid-template-columns: minmax(200px, 1fr) 90px 70px 120px 120px;
}
.weight-input {
  width: 90px;
}
/* 弹窗内的开关行：左标签、右控件，与表单字段同节奏 */
.switch-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
  margin: var(--sp-3) 0 var(--sp-4);
  font-size: 13px;
  color: var(--text-secondary);
}

/* --- Workflow list --- */
.wf-groups {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding-top: 12px;
}
.wf-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 0 2px 6px;
}
.wf-group-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.wf-group-count {
  min-width: 18px;
  padding: 0 6px;
  font-size: 11px;
  line-height: 17px;
  text-align: center;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border-radius: var(--radius-sm);
}
.wf-group-hint {
  font-size: 12px;
  color: var(--state-warning);
}
.wf-sub {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.col-state {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.wf-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
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
  flex: none;
  white-space: nowrap;
  padding: var(--sp-2) var(--sp-3);
  font-family: inherit;
  font-size: 13px;
  line-height: 20px;
  color: var(--text-secondary);
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.wf-type-tab:hover {
  color: var(--text-primary);
}
.wf-type-tab:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: -2px;
}
.wf-type-tab.active {
  color: var(--brand-primary);
  border-bottom-color: var(--brand-primary);
  font-weight: 600;
}
/* 加载态：与工作流行同高的骨架条，不用转圈遮罩 */
.wf-skeleton {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-top: var(--sp-3);
}
.wf-skeleton .sk-row {
  height: 44px;
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
  gap: var(--sp-2);
  margin-bottom: var(--sp-1);
}
.field-label {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}
/* 表单字段的补充说明（如视频模型类型的预留能力提示） */
.field-hint {
  margin-top: var(--sp-1);
  font-size: 12px;
  color: var(--text-tertiary);
}
.field-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
}
.field-grow {
  flex: 1;
  min-width: 0;
}
.field-full {
  width: 100%;
}
/* 弹窗操作区：次要动作靠左，取消在左、主操作在右（DESIGN §13） */
.modal-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  margin-top: var(--sp-4);
}
.modal-actions .spacer {
  flex: 1;
}
.modal-hint {
  margin: 0 0 var(--sp-3);
}
.modal-hint-sub {
  margin: var(--sp-1) 0 0;
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
.hint-text.is-danger {
  color: var(--danger-text);
}
/* 卡片内空态：用凹陷底，避免面板里再叠一层亮面板 */
.in-card-empty {
  margin-top: var(--sp-1);
  background: var(--bg-sunken);
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
  justify-content: flex-end;
  gap: var(--sp-3);
  margin-bottom: var(--sp-3);
}
.llm-mini-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--sp-3);
}
.llm-mini-card {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  background: var(--bg-panel);
  transition: background var(--transition-fast), border-color var(--transition-fast);
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
@media (max-width: 1100px) {
  .settings-layout {
    gap: var(--sp-4);
    padding: var(--sp-5);
  }
}
@media (max-width: 768px) {
  .settings-layout {
    flex-direction: column;
    padding: var(--sp-3);
    gap: var(--sp-4);
  }
  /* 竖排 4 项要吃掉约 200px 首屏高度，且说明文字与档名重复：
     窄屏改为横向分段，只留档名，可横向滚动。 */
  .settings-nav {
    width: 100%;
    flex-direction: row;
    align-items: center;
    gap: var(--sp-1);
    padding: var(--sp-2);
    overflow-x: auto;
    scrollbar-width: none;
  }
  .settings-nav::-webkit-scrollbar {
    display: none;
  }
  .nav-header {
    display: none;
  }
  .nav-item {
    flex: 0 0 auto;
    width: auto;
    padding: var(--sp-2) var(--sp-3);
  }
  .nav-desc {
    display: none;
  }
  .nav-label {
    font-size: 13.5px;
  }
  /* 筛选 tab 与主按钮同排时会被挤成两字一行：窄屏让按钮留在首行右侧，
     tab 独占第二行，避免文字竖排。 */
  .wf-toolbar {
    flex-wrap: wrap;
    row-gap: var(--sp-2);
  }
  .wf-type-tabs {
    order: 2;
    width: 100%;
    flex: 1 0 100%;
    scrollbar-width: none;
  }
  .wf-type-tabs::-webkit-scrollbar {
    display: none;
  }
  .wf-toolbar > .n-button {
    order: 1;
    margin-left: auto;
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
