<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton,
  NDropdown,
  NEmpty,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NProgress,
  NSelect,
  NTag,
  useDialog,
  useMessage,
} from 'naive-ui'
import type { DropdownOption, FormInst, FormRules } from 'naive-ui'
import { archiveProject, createProject, deleteProject, getPurgePreview, getStyleOptions, listProjects, purgeProject } from '@/api/projects'
import type { PurgePreview } from '@/api/projects'
import { getSettings, listComfyuiInstances } from '@/api/settings'
import { createSeries } from '@/api/series'
import type { SeriesCreatePayload } from '@/types/series'
import type { AspectRatio, CreationType, CreateProjectPayload, Project, StyleConfig } from '@/types/project'
import StyleConfigForm from '@/components/StyleConfigForm.vue'
const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const queryClient = useQueryClient()
/* ---------- 常量 ---------- */
const STATUS_META: Record<Project['status'], { label: string; type: 'default' | 'success' | 'warning' }> = {
  draft: { label: '草稿', type: 'warning' },
  active: { label: '进行中', type: 'success' },
  archived: { label: '已归档', type: 'default' },
}
const CREATION_LABEL: Record<CreationType, string> = {
  novel: '小说改编',
  script: '剧本直出',
}
const ASPECT_OPTIONS: AspectRatio[] = ['9:16', '16:9', '1:1']
const CREATION_OPTIONS: { value: CreationType; label: string; desc: string }[] = [
  { value: 'novel', label: '小说改编', desc: '从小说文本生成剧本' },
  { value: 'script', label: '剧本直出', desc: '直接编写剧本内容' },
]
/* ---------- 配置状态（未配置时顶部引导横幅） ---------- */
const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: () => getSettings() })
const instancesQuery = useQuery({ queryKey: ['comfyui-instances'], queryFn: () => listComfyuiInstances() })
const llmReady = computed(() => {
  const s: any = settingsQuery.data.value
  return Boolean(s && s.llm_base_url && s.llm_model && s.llm_api_key_set)
})
const comfyReady = computed(() => {
  const list = instancesQuery.data.value
  return Array.isArray(list) && list.some((i: any) => i.enabled)
})
const missingConfigs = computed(() => {
  const items: string[] = []
  if (!llmReady.value) items.push('语言模型')
  if (!comfyReady.value) items.push('ComfyUI')
  return items
})
/* ---------- 列表查询 ---------- */
const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['projects'],
  queryFn: () => listProjects({ page: 1, page_size: 100 }),
})
const projects = computed(() => data.value?.items ?? [])
function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败，请稍后重试'
}
/* ---------- 搜索 / 筛选 / 排序 ---------- */
const searchKeyword = ref('')
type StatusFilter = 'all' | Project['status']
const statusFilter = ref<StatusFilter>('all')
const sortKey = ref<'updated' | 'name'>('updated')
const FILTER_CHIPS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'draft', label: '草稿' },
  { value: 'active', label: '进行中' },
  { value: 'archived', label: '已归档' },
]
const filteredProjects = computed(() => {
  let list = projects.value
  const kw = searchKeyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((p) => p.name.toLowerCase().includes(kw))
  }
  if (statusFilter.value !== 'all') {
    list = list.filter((p) => p.status === statusFilter.value)
  }
  const sorted = [...list]
  if (sortKey.value === 'name') {
    sorted.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
  } else {
    sorted.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
  }
  return sorted
})
const activeCount = computed(() => projects.value.filter((p) => p.status === 'active').length)
/* ---------- 变更操作 ---------- */
function invalidateProjects() {
  void queryClient.invalidateQueries({ queryKey: ['projects'] })
}
const { mutate: mutateCreate, isPending: creating } = useMutation({
  mutationFn: createProject,
  onSuccess: (p) => {
    message.success(`作品「${p.name}」创建成功`)
    showModal.value = false
    invalidateProjects()
  },
  onError: (e) => {
    // 配置未完成：阻断并引导用户前往设置页
    const code = (e as { code?: string })?.code
    if (code === 'llm_not_configured' || code === 'comfyui_not_configured') {
      const isLlm = code === 'llm_not_configured'
      dialog.warning({
        title: isLlm ? '语言模型未配置' : 'ComfyUI 未配置',
        content: isLlm
          ? '创建作品前需要先完成语言模型配置（API URL / API Key / 模型名称）。是否前往设置页面进行配置？'
          : '创建作品前需要先完成 ComfyUI 配置（至少一个启用实例）。是否前往设置页面进行配置？',
        positiveText: '前往设置',
        negativeText: '暂不创建',
        onPositiveClick: () => {
          void router.push('/settings')
        },
      })
      return
    }
    message.error(errMsg(e))
  },
})
const { mutate: mutateCreateSeries, isPending: creatingSeries } = useMutation({
  mutationFn: (payload: SeriesCreatePayload) => createSeries(payload),
  onSuccess: (s) => {
    message.success(`系列「${s.name}」创建成功`)
    showModal.value = false
    void router.push({ name: 'series-detail', params: { id: s.id } })
  },
  onError: (e) => {
    message.error(errMsg(e))
  },
})
const { mutate: mutateArchive } = useMutation({
  mutationFn: archiveProject,
  onSuccess: () => {
    message.success('作品已归档')
    invalidateProjects()
  },
  onError: (e) => message.error(errMsg(e)),
})
const { mutate: mutateDelete } = useMutation({
  mutationFn: deleteProject,
  onSuccess: () => {
    message.success('作品已删除')
    invalidateProjects()
  },
  onError: (e) => message.error(errMsg(e)),
})
const { mutate: mutatePurge } = useMutation({
  mutationFn: purgeProject,
  onSuccess: (res) => {
    const rows = Object.values(res?.deleted ?? {}).reduce((a, b) => a + b, 0)
    const files = res?.media?.removed ?? 0
    message.success(`作品已彻底删除：清理 ${rows} 条记录、${files} 个媒体文件`)
    invalidateProjects()
  },
  onError: (e) => message.error(errMsg(e)),
})
/* ---------- 卡片操作 ---------- */
function goProject(id: string) {
  void router.push(`/project/${id}/script`)
}
function menuOptions(p: Project): DropdownOption[] {
  return [
    { key: 'copy', label: '复制（后续版本支持）', disabled: true },
    { key: 'archive', label: '归档', disabled: p.status === 'archived' },
    { key: 'delete', label: '删除（仅从列表移除）', props: { style: 'color: var(--danger);' } },
    { key: 'purge', label: '彻底删除（含媒体文件）', props: { style: 'color: var(--danger);' } },
  ]
}
function onMenuSelect(key: string, p: Project) {
  if (key === 'archive') {
    mutateArchive(p.id)
  } else if (key === 'delete') {
    confirmDelete(p)
  } else if (key === 'purge') {
    void confirmPurge(p)
  }
}
function onDropdownSelect(p: Project) {
  return (key: string | number) => onMenuSelect(String(key), p)
}
function confirmDelete(p: Project) {
  dialog.warning({
    title: '删除作品',
    content: `确定要删除作品「${p.name}」吗？删除后不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    class: 'danger-dialog',
    onPositiveClick: () => {
      mutateDelete(p.id)
    },
  })
}

async function confirmPurge(p: Project) {
  let preview: PurgePreview
  try {
    preview = await getPurgePreview(p.id)
  } catch (e) {
    message.error(errMsg(e))
    return
  }
  if (!preview.deletable) {
    dialog.error({
      title: '暂时无法彻底删除',
      content: `作品「${p.name}」还有 ${preview.blocking_jobs} 个进行中的任务（排队 / 运行 / 提交待确认 / 链式等待），请先到任务页取消或等待完成后再试。`,
      positiveText: '知道了',
    })
    return
  }
  const c = preview.counts
  dialog.error({
    title: '彻底删除作品（不可恢复）',
    content: `即将永久删除作品「${p.name}」及其全部内容：

· 场景 ${c.scenes} 个、镜头 ${c.shots} 个
· 资产 ${c.assets} 个（含分镜图、角色图、视频等媒体文件）
· 任务记录 ${c.jobs} 条
· 角色 ${c.characters} 个、道具 ${c.props} 个
· 剧本版本 ${c.script_versions} 个

数据库记录与磁盘上的媒体文件都会被清除，此操作无法撤销。`,
    positiveText: '确认彻底删除',
    negativeText: '取消',
    class: 'danger-dialog',
    onPositiveClick: () => {
      mutatePurge(p.id)
    },
  })
}
/* ---------- 进度与时间 ---------- */
function progressOf(p: Project): number {
  if (!p.episode_plan || p.episode_plan.length === 0) return 0
  const planned = p.episode_plan.filter((e) => (e.shot_count ?? 0) > 0).length
  return Math.round((planned / p.episode_plan.length) * 100)
}
function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}
function coverChar(p: Project): string {
  const ch = p.name.trim().charAt(0)
  return ch ? ch.toUpperCase() : 'P'
}
/* ---------- 新建作品弹窗 ---------- */
const showModal = ref(false)
const createType = ref<'project' | 'series'>('project')
const formRef = ref<FormInst | null>(null)
const seriesFormRef = ref<FormInst | null>(null)
const formModel = ref<{ name: string; aspect_ratio: AspectRatio; creation_type: CreationType }>({
  name: '',
  aspect_ratio: '16:9',
  creation_type: 'novel',
})
const seriesForm = ref<{
  name: string
  description: string
  genre: string | null
  style: string | null
  target_audience: string | null
}>({
  name: '',
  description: '',
  genre: '',
  style: '',
  target_audience: '',
})
/* ---------- 创建时可选风格配置（留空则完全使用后端默认值） ---------- */
const styleOptionsQuery = useQuery({
  queryKey: ['style-options'],
  queryFn: () => getStyleOptions(),
  staleTime: 5 * 60 * 1000,
})
const styleOptions = computed(() => styleOptionsQuery.data.value)
/** 系列表单下拉选项：与单集项目的风格配置共用后端选项，避免人工填写口径不一致 */
const seriesGenreOptions = computed(() =>
  (styleOptions.value?.genre_groups ?? []).map((g) => ({
    type: 'group' as const,
    label: g.group,
    key: g.group,
    children: g.tags.map((t) => ({ label: t, value: t })),
  })),
)
const seriesStyleOptions = computed(() =>
  (styleOptions.value?.visual_styles ?? []).map((s) => ({ label: s.label, value: s.label })),
)
const seriesAudienceOptions = computed(() =>
  (styleOptions.value?.target_audiences ?? []).map((a) => ({ label: a.label, value: a.label })),
)
const seriesStyleHint = computed(() => {
  const key = seriesForm.value.style
  return (styleOptions.value?.visual_styles ?? []).find((s) => s.label === key)?.description ?? ''
})
const showStyleConfig = ref(false)
const styleConfig = ref<StyleConfig | null>(null)
function toggleStyleConfig(v: boolean) {
  showStyleConfig.value = v
  if (v && !styleConfig.value && styleOptions.value) {
    styleConfig.value = { ...styleOptions.value.defaults }
  }
}
const rules: FormRules = {
  name: [
    { required: true, message: '请输入作品名称', trigger: ['blur', 'input'] },
    {
      validator: (_rule, value: string) => value.trim().length >= 1 && value.trim().length <= 100,
      message: '作品名称需为 1-100 个字符',
      trigger: ['blur', 'input'],
    },
  ],
}
const seriesRules: FormRules = {
  name: [
    { required: true, message: '请输入系列名称', trigger: ['blur', 'input'] },
    {
      validator: (_rule, value: string) => value.trim().length >= 1 && value.trim().length <= 200,
      message: '系列名称需为 1-200 个字符',
      trigger: ['blur', 'input'],
    },
  ],
  description: [{ max: 2000, message: '简介不能超过 2000 个字符', trigger: ['input', 'blur'] }],
}
function openCreateModal() {
  createType.value = 'project'
  formModel.value = { name: '', aspect_ratio: '16:9', creation_type: 'novel' }
  seriesForm.value = { name: '', description: '', genre: '', style: '', target_audience: '' }
  showStyleConfig.value = false
  styleConfig.value = styleOptions.value ? { ...styleOptions.value.defaults } : null
  showModal.value = true
}
function onUpdateShow(v: boolean) {
  if (v) {
    showModal.value = true
    return
  }
  if (formModel.value.name.trim() !== '') {
    dialog.warning({
      title: '放弃未保存的修改？',
      content: '当前表单已填写内容，关闭后将丢失。',
      positiveText: '放弃修改',
      negativeText: '继续编辑',
      onPositiveClick: () => {
        showModal.value = false
      },
    })
  } else {
    showModal.value = false
  }
}
async function handleCreate() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  const payload: CreateProjectPayload = {
    name: formModel.value.name.trim(),
    aspect_ratio: formModel.value.aspect_ratio,
    creation_type: formModel.value.creation_type,
  }
  // 仅当用户主动展开并修改过风格配置时才提交，避免覆盖后端默认基线
  if (showStyleConfig.value && styleConfig.value) {
    payload.style_config = { ...styleConfig.value }
  }
  mutateCreate(payload)
}
async function handleCreateSeries() {
  try {
    await seriesFormRef.value?.validate()
  } catch {
    return
  }
  const payload: SeriesCreatePayload = {
    name: seriesForm.value.name.trim(),
    description: (seriesForm.value.description ?? '').trim() || null,
    genre: (seriesForm.value.genre ?? '').trim() || null,
    style: (seriesForm.value.style ?? '').trim() || null,
    target_audience: (seriesForm.value.target_audience ?? '').trim() || null,
  }
  mutateCreateSeries(payload)
}
</script>
<template>
  <section class="page project-list-page">
    <header class="page-header">
      <div class="header-left">
        <div v-if="projects.length" class="header-stats">
          <span class="stat-chip">{{ projects.length }} 个作品</span>
          <span class="stat-chip stat-active">{{ activeCount }} 个进行中</span>
        </div>
      </div>
      <div class="header-actions">
        <n-input
          v-model:value="searchKeyword"
          placeholder="搜索作品"
          clearable
          class="search-input"
          size="small"
        />
        <n-button type="primary" @click="openCreateModal">新建作品</n-button>
      </div>
    </header>
    <div v-if="missingConfigs.length" class="config-banner">
      <span class="banner-text">
        尚未完成 {{ missingConfigs.join('、') }} 配置，创建作品前请先完成配置
      </span>
      <n-button size="small" type="primary" @click="router.push('/settings')">前往设置</n-button>
    </div>
    <div v-if="isError" class="state-block">
      <p class="state-text">加载失败：{{ errMsg(error) }}</p>
      <n-button secondary @click="() => refetch()">重试</n-button>
    </div>
    <div v-else-if="isPending" class="project-grid">
      <div v-for="i in 8" :key="i" class="skeleton-card">
        <div class="skeleton-cover"></div>
        <div class="skeleton-body">
          <div class="skeleton-line w-60"></div>
          <div class="skeleton-line w-40"></div>
          <div class="skeleton-line w-100"></div>
        </div>
      </div>
    </div>
    <template v-else>
      <div v-if="projects.length === 0" class="state-block">
        <n-empty description="还没有作品，创建一个开始你的漫剧创作吧">
          <template #extra>
            <n-button type="primary" @click="openCreateModal">新建作品</n-button>
          </template>
        </n-empty>
      </div>
      <template v-else>
        <div class="toolbar">
          <div class="filter-chips">
            <button
              v-for="c in FILTER_CHIPS"
              :key="c.value"
              type="button"
              class="filter-chip"
              :class="{ active: statusFilter === c.value }"
              @click="statusFilter = c.value"
            >
              {{ c.label }}
            </button>
          </div>
          <div class="sort-group">
            <button
              type="button"
              class="sort-btn"
              :class="{ active: sortKey === 'updated' }"
              @click="sortKey = 'updated'"
            >最近更新</button>
            <button
              type="button"
              class="sort-btn"
              :class="{ active: sortKey === 'name' }"
              @click="sortKey = 'name'"
            >名称</button>
          </div>
        </div>
        <div v-if="filteredProjects.length === 0" class="state-block">
          <n-empty description="没有匹配的作品，试试调整搜索关键词或筛选条件">
            <template #extra>
              <n-button secondary @click="searchKeyword = ''; statusFilter = 'all'">清除筛选</n-button>
            </template>
          </n-empty>
        </div>
        <div v-else class="project-grid">
          <button type="button" class="launch-card" @click="openCreateModal">
            <span class="launch-plus">+</span>
            <span class="launch-text">新建作品</span>
            <span class="launch-sub">开始新的漫剧创作</span>
          </button>
          <article
            v-for="(p, idx) in filteredProjects"
            :key="p.id"
            class="project-card"
            :style="{ animationDelay: `${Math.min(idx, 12) * 0.04}s` }"
            @click="goProject(p.id)"
          >
            <div class="cover" :class="`cover-${p.aspect_ratio.replace(':', '-')}`">
              <span class="cover-char">{{ coverChar(p) }}</span>
              <n-tag class="status-tag" :type="STATUS_META[p.status].type" size="small" round>
                {{ STATUS_META[p.status].label }}
              </n-tag>
              <span class="cover-ratio">{{ p.aspect_ratio }}</span>
            </div>
            <div class="card-body">
              <div class="card-title-row">
                <h3 class="card-title" :title="p.name">{{ p.name }}</h3>
                <n-dropdown
                  trigger="click"
                  :options="menuOptions(p)"
                  @select="onDropdownSelect(p)($event)"
                  @click.stop
                >
                  <n-button quaternary size="tiny" class="more-btn" @click.stop>⋯</n-button>
                </n-dropdown>
              </div>
              <div class="card-meta">
                <span>{{ CREATION_LABEL[p.creation_type] }}</span>
                <span :title="new Date(p.updated_at).toLocaleString('zh-CN')">
                  更新于 {{ relativeTime(p.updated_at) }}
                </span>
              </div>
              <div class="progress-row">
                <n-progress
                  class="progress-bar"
                  type="line"
                  :percentage="progressOf(p)"
                  :height="6"
                  :border-radius="3"
                  :show-indicator="false"
                  color="var(--brand-primary)"
                  rail-color="var(--border-subtle)"
                />
                <span class="progress-text">{{ progressOf(p) }}%</span>
              </div>
            </div>
          </article>
        </div>
      </template>
    </template>
    <!-- ===== 新建作品弹窗 ===== -->
    <n-modal
      :show="showModal"
      :on-update:show="onUpdateShow"
      preset="card"
      title="新建作品"
      class="create-modal"
      :style="{ width: '520px' }"
      :mask-closable="false"
    >
      <!-- ===== 创建类型选择 ===== -->
      <div class="create-type-cards">
        <button
          type="button"
          class="create-type-card"
          :class="{ active: createType === 'project' }"
          @click="createType = 'project'"
        >
          <span class="ct-icon">🎬</span>
          <span class="ct-name">单集项目</span>
          <span class="ct-desc">独立的单集漫剧，快速从剧本到成片</span>
        </button>
        <button
          type="button"
          class="create-type-card"
          :class="{ active: createType === 'series' }"
          @click="createType = 'series'"
        >
          <span class="ct-icon">📚</span>
          <span class="ct-name">系列</span>
          <span class="ct-desc">多集长篇创作，统一世界观与角色资产</span>
        </button>
      </div>

      <!-- ===== 单集项目表单 ===== -->
      <n-form
        v-if="createType === 'project'"
        ref="formRef"
        :model="formModel"
        :rules="rules"
        label-placement="top"
        :show-require-mark="false"
      >
        <n-form-item label="作品名称" path="name">
          <n-input
            v-model:value="formModel.name"
            placeholder="请输入作品名称（1-100 字符）"
            maxlength="100"
          />
        </n-form-item>
        <n-form-item label="画幅比" path="aspect_ratio">
          <div class="ratio-cards">
            <button
              v-for="r in ASPECT_OPTIONS"
              :key="r"
              type="button"
              class="ratio-card"
              :class="{ active: formModel.aspect_ratio === r }"
              @click="formModel.aspect_ratio = r"
            >
              <span class="ratio-shape" :class="`shape-${r.replace(':', '-')}`"></span>
              <span class="ratio-label">{{ r }}</span>
            </button>
          </div>
        </n-form-item>
        <n-form-item label="创作类型" path="creation_type">
          <div class="type-cards">
            <button
              v-for="t in CREATION_OPTIONS"
              :key="t.value"
              type="button"
              class="type-card"
              :class="{ active: formModel.creation_type === t.value }"
              @click="formModel.creation_type = t.value"
            >
              <span class="type-name">{{ t.label }}</span>
              <span class="type-desc">{{ t.desc }}</span>
            </button>
          </div>
        </n-form-item>
        <div class="style-section">
          <button
            type="button"
            class="style-toggle"
            :class="{ open: showStyleConfig }"
            @click="toggleStyleConfig(!showStyleConfig)"
          >
            <span class="toggle-title">剧本类型与风格配置</span>
            <span class="toggle-hint">可选。不设置则使用默认风格，创建后也可在剧本页调整。</span>
            <span class="toggle-arrow">{{ showStyleConfig ? '收起' : '展开' }}</span>
          </button>
          <div v-if="showStyleConfig" class="style-body">
            <div v-if="!styleOptions" class="style-loading">正在加载风格选项…</div>
            <StyleConfigForm
              v-else-if="styleConfig"
              v-model="styleConfig"
              :options="styleOptions"
            />
          </div>
        </div>
      </n-form>

      <!-- ===== 系列表单 ===== -->
      <n-form
        v-else
        ref="seriesFormRef"
        :model="seriesForm"
        :rules="seriesRules"
        label-placement="top"
        :show-require-mark="false"
      >
        <n-form-item label="系列名称" path="name">
          <n-input
            v-model:value="seriesForm.name"
            placeholder="例如：小熊成长记"
            maxlength="200"
            show-count
          />
        </n-form-item>
        <n-form-item label="系列简介" path="description">
          <n-input
            v-model:value="seriesForm.description"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="简述系列的主题、世界观与整体走向"
            maxlength="2000"
            show-count
          />
        </n-form-item>
        <div class="form-row">
          <n-form-item label="题材类型" path="genre" class="form-row-item">
            <n-select
              v-model:value="seriesForm.genre"
              :options="seriesGenreOptions"
              placeholder="选择题材类型"
              filterable
              clearable
            />
          </n-form-item>
          <n-form-item label="目标受众" path="target_audience" class="form-row-item">
            <n-select
              v-model:value="seriesForm.target_audience"
              :options="seriesAudienceOptions"
              placeholder="选择目标受众"
              filterable
              clearable
            />
          </n-form-item>
        </div>
        <n-form-item label="整体风格" path="style">
          <div class="series-style-field">
            <n-select
              v-model:value="seriesForm.style"
              :options="seriesStyleOptions"
              placeholder="选择整体视觉风格"
              filterable
              clearable
            />
            <div v-if="seriesStyleHint" class="series-style-hint">{{ seriesStyleHint }}</div>
          </div>
        </n-form-item>
      </n-form>

      <template #footer>
        <div class="modal-footer">
          <n-button @click="onUpdateShow(false)">取消</n-button>
          <n-button
            v-if="createType === 'project'"
            type="primary"
            :loading="creating"
            @click="handleCreate"
          >创建作品</n-button>
          <n-button
            v-else
            type="primary"
            :loading="creatingSeries"
            @click="handleCreateSeries"
          >创建系列</n-button>
        </div>
      </template>
    </n-modal>
  </section>
</template>
<style scoped>
.project-list-page {
  --text-primary: #e5e7eb;
  --text-secondary: #9ca3af;
  min-height: calc(100vh - var(--app-header-height, 56px));
  padding: 24px;
  max-width: 1440px;
  margin: 0 auto;
  background: var(--bg-base);
  color: var(--text-primary);
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 4px 0 20px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.header-stats {
  display: flex;
  align-items: center;
  gap: 8px;
}
.stat-chip {
  padding: 3px 10px;
  font-size: 12px;
  line-height: 18px;
  border-radius: 999px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  color: var(--text-secondary);
}
.stat-chip.stat-active {
  color: #34d399;
  border-color: rgba(52, 211, 153, 0.35);
  background: rgba(52, 211, 153, 0.08);
}
/* Tab 切换 */
.tab-switch {
  display: flex;
  gap: 2px;
  padding: 2px;
  border-radius: 8px;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
}
.tab-btn {
  padding: 4px 16px;
  font-size: 13px;
  font-weight: 500;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background 0.2s, color 0.2s;
}
.tab-btn.active {
  background: var(--brand-primary);
  color: #fff;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
/* 系列卡片网格 */
.series-tab-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}
.series-card {
  padding: 20px;
  border-radius: 12px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  cursor: pointer;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.series-card:hover {
  border-color: var(--brand-primary);
  box-shadow: 0 2px 12px rgba(53, 179, 166, 0.12);
}
.series-card .card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.series-card .card-head h2 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}
.series-card .desc {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.series-card .stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 12px;
}
.series-card .card-actions {
  display: flex;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}
.config-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  margin-bottom: 20px;
  border: 1px solid rgba(245, 158, 11, 0.35);
  background: rgba(245, 158, 11, 0.08);
  border-radius: 8px;
}
.banner-text {
  font-size: 13px;
  color: var(--text-primary);
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.search-input {
  width: 240px;
}
.search-icon {
  font-size: 15px;
  color: var(--text-secondary);
}
.filter-chips {
  display: flex;
  align-items: center;
  gap: 6px;
}
.filter-chip {
  padding: 4px 14px;
  font-size: 13px;
  line-height: 20px;
  border-radius: 999px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
  transition:
    border-color 0.2s ease,
    color 0.2s ease,
    background-color 0.2s ease;
}
.filter-chip:hover {
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.filter-chip.active {
  border-color: var(--brand-primary);
  color: var(--brand-primary);
  background: rgba(53, 179, 166, 0.12);
}
.sort-group {
  display: flex;
  align-items: center;
  margin-left: auto;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  overflow: hidden;
}
.sort-btn {
  padding: 4px 14px;
  font-size: 13px;
  line-height: 20px;
  border: none;
  background: var(--bg-panel);
  color: var(--text-secondary);
  cursor: pointer;
  transition:
    color 0.2s ease,
    background-color 0.2s ease;
}
.sort-btn + .sort-btn {
  border-left: 1px solid var(--border-subtle);
}
.sort-btn.active {
  color: var(--brand-primary);
  background: rgba(53, 179, 166, 0.12);
}
.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  padding: 64px 0;
}
.state-text {
  margin: 0;
  font-size: 14px;
  color: var(--text-secondary);
}
.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
  gap: 16px;
}
.launch-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 240px;
  border: 1.5px dashed var(--border-strong);
  border-radius: 14px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease,
    color 0.2s ease;
}
.launch-card:hover {
  border-color: var(--brand-primary);
  background: rgba(53, 179, 166, 0.06);
  color: var(--brand-primary);
}
.launch-plus {
  font-size: 32px;
  line-height: 1;
  font-weight: 300;
}
.launch-text {
  font-size: 14px;
  font-weight: 500;
}
.launch-sub {
  font-size: 12px;
}
.project-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  overflow: hidden;
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
  transition:
    box-shadow 0.2s ease,
    border-color 0.2s ease,
    transform 0.2s ease;
  animation: card-fade-up 0.35s ease both;
}
@keyframes card-fade-up {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .project-card {
    animation: none;
  }
}
.project-card:hover {
  border-color: var(--brand-primary);
  box-shadow: 0 2px 8px rgba(53, 179, 166, 0.15);
  transform: translateY(-2px);
}
.cover {
  position: relative;
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
}
.cover-char {
  font-size: 44px;
  font-weight: 700;
  color: rgba(255, 255, 255, 0.9);
  text-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
  user-select: none;
}
.cover-9-16 {
  background: #17332f;
}
.cover-16-9 {
  background: #142a2e;
}
.cover-1-1 {
  background: #1a2b30;
}
.status-tag {
  position: absolute;
  top: 12px;
  left: 12px;
}
.cover-ratio {
  position: absolute;
  right: 12px;
  bottom: 8px;
  font-size: 12px;
  line-height: 18px;
  color: rgba(255, 255, 255, 0.85);
  letter-spacing: 0.5px;
}
.card-body {
  padding: 12px;
}
.card-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.card-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 18px;
  line-height: 26px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.more-btn {
  color: var(--text-secondary);
}
.card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 4px;
  font-size: 14px;
  line-height: 20px;
  color: var(--text-secondary);
}
.progress-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.progress-bar {
  flex: 1;
}
.progress-text {
  font-size: 13px;
  line-height: 18px;
  color: var(--text-secondary);
  min-width: 34px;
  text-align: right;
}
.skeleton-card {
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  background: var(--bg-panel);
  overflow: hidden;
}
.skeleton-cover {
  aspect-ratio: 16 / 9;
  background: linear-gradient(100deg, var(--bg-elevated) 40%, var(--bg-panel) 50%, var(--bg-elevated) 60%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s ease infinite;
}
.skeleton-body {
  padding: 14px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.skeleton-line {
  height: 12px;
  border-radius: 6px;
  background: linear-gradient(100deg, var(--bg-elevated) 40%, var(--bg-panel) 50%, var(--bg-elevated) 60%);
  background-size: 200% 100%;
  animation: skeleton-shimmer 1.4s ease infinite;
}
.w-40 { width: 40%; }
.w-60 { width: 60%; }
.w-100 { width: 100%; }
@keyframes skeleton-shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
@media (prefers-reduced-motion: reduce) {
  .skeleton-cover,
  .skeleton-line {
    animation: none;
  }
}
.ratio-cards {
  display: flex;
  gap: 12px;
  width: 100%;
}
.ratio-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  cursor: pointer;
  color: var(--text-primary);
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease;
}
.ratio-card:hover {
  border-color: var(--border-strong);
}
.ratio-card.active {
  border-color: var(--brand-primary);
  background: rgba(53, 179, 166, 0.12);
}
.ratio-shape {
  display: block;
  border-radius: 4px;
  background: var(--brand-primary);
}
.shape-9-16 {
  width: 18px;
  height: 32px;
}
.shape-16-9 {
  width: 32px;
  height: 18px;
}
.shape-1-1 {
  width: 26px;
  height: 26px;
}
.ratio-label {
  font-size: 13px;
  line-height: 20px;
}
.type-cards {
  display: flex;
  gap: 12px;
  width: 100%;
}
.type-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px;
  text-align: left;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  cursor: pointer;
  color: var(--text-primary);
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease;
}
.type-card:hover {
  border-color: var(--border-strong);
}
.type-card.active {
  border-color: var(--brand-primary);
  background: rgba(53, 179, 166, 0.12);
}
.type-name {
  font-size: 14px;
  line-height: 22px;
  font-weight: 500;
}
.type-desc {
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
/* ===== 创建类型选择卡 ===== */
.create-type-cards {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.create-type-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 14px;
  text-align: left;
  background: var(--bg-elevated, #f8fafc);
  border: 1.5px solid var(--border-subtle, #e5e7eb);
  border-radius: 10px;
  cursor: pointer;
  color: var(--text-primary, #111827);
  transition: border-color 0.2s ease, background-color 0.2s ease;
}
.create-type-card:hover {
  border-color: var(--border-strong, #cbd5e1);
}
.create-type-card.active {
  border-color: var(--brand-primary);
  background: rgba(53, 179, 166, 0.1);
}
.ct-icon {
  font-size: 22px;
  line-height: 1;
}
.ct-name {
  margin-top: 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary, #111827);
}
.ct-desc {
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary, #6b7280);
}
/* ===== 系列表单两列 ===== */
.form-row {
  display: flex;
  gap: 12px;
}
.form-row-item {
  flex: 1;
  min-width: 0;
}
.series-style-field {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.series-style-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
/* ===== 系列引导（已弃用，保留占位防样式漂移）===== */
/* 新建作品弹窗内的可选风格配置折叠区 */
.style-section {
  margin-top: 4px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle, #e5e7eb);
}
.style-toggle {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border-subtle, #e5e7eb);
  background: var(--bg-elevated, #f8fafc);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s;
}
.style-toggle:hover,
.style-toggle.open {
  border-color: var(--brand-primary);
}
.toggle-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.toggle-hint {
  flex: 1 1 auto;
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
}
.toggle-arrow {
  font-size: 12px;
  color: var(--brand-primary);
  white-space: nowrap;
}
.style-body {
  margin-top: 12px;
  max-height: 46vh;
  overflow-y: auto;
  padding-right: 4px;
}
.style-loading {
  font-size: 13px;
  color: var(--text-secondary);
  padding: 12px 0;
}
</style>
<style>
/* 删除确认弹窗：确认按钮使用危险色（danger var(--danger)） */
.n-dialog.danger-dialog .n-button--primary-type {
  --n-color: var(--danger) !important;
  --n-color-hover: #f87171 !important;
  --n-color-pressed: #dc2626 !important;
  --n-color-focus: #f87171 !important;
  --n-text-color: #ffffff !important;
  --n-text-color-hover: #ffffff !important;
  --n-text-color-pressed: #ffffff !important;
  --n-text-color-focus: #ffffff !important;
  --n-border: 1px solid transparent !important;
  --n-border-hover: 1px solid transparent !important;
  --n-border-pressed: 1px solid transparent !important;
  --n-border-focus: 1px solid transparent !important;
}
</style>
