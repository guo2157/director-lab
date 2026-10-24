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
  NSpin,
  NTag,
  useDialog,
  useMessage,
} from 'naive-ui'
import type { DropdownOption, FormInst, FormRules } from 'naive-ui'
import { archiveProject, createProject, deleteProject, listProjects } from '@/api/projects'
import type { AspectRatio, CreationType, Project } from '@/types/project'

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

/* ---------- 列表查询 ---------- */
const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['projects'],
  queryFn: () => listProjects({ page: 1, page_size: 100 }),
})
const projects = computed(() => data.value?.items ?? [])

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败，请稍后重试'
}

/* ---------- 变更操作 ---------- */
function invalidateProjects() {
  void queryClient.invalidateQueries({ queryKey: ['projects'] })
}

const { mutate: mutateCreate, isPending: creating } = useMutation({
  mutationFn: createProject,
  onSuccess: (p) => {
    message.success(`项目「${p.name}」创建成功`)
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
          ? '创建项目前需要先完成语言模型配置（API URL / API Key / 模型名称）。是否前往设置页面进行配置？'
          : '创建项目前需要先完成 ComfyUI 配置（至少一个启用实例）。是否前往设置页面进行配置？',
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

const { mutate: mutateArchive } = useMutation({
  mutationFn: archiveProject,
  onSuccess: () => {
    message.success('项目已归档')
    invalidateProjects()
  },
  onError: (e) => message.error(errMsg(e)),
})

const { mutate: mutateDelete } = useMutation({
  mutationFn: deleteProject,
  onSuccess: () => {
    message.success('项目已删除')
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
    { key: 'delete', label: '删除', props: { style: 'color: var(--danger);' } },
  ]
}

function onMenuSelect(key: string, p: Project) {
  if (key === 'archive') {
    mutateArchive(p.id)
  } else if (key === 'delete') {
    confirmDelete(p)
  }
}

function onDropdownSelect(p: Project) {
  return (key: string | number) => onMenuSelect(String(key), p)
}

function confirmDelete(p: Project) {
  dialog.warning({
    title: '删除项目',
    content: `确定要删除项目「${p.name}」吗？删除后不可恢复。`,
    positiveText: '删除',
    negativeText: '取消',
    class: 'danger-dialog',
    onPositiveClick: () => {
      mutateDelete(p.id)
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

/* ---------- 新建项目弹窗 ---------- */
const showModal = ref(false)
const formRef = ref<FormInst | null>(null)
const formModel = ref<{ name: string; aspect_ratio: AspectRatio; creation_type: CreationType }>({
  name: '',
  aspect_ratio: '16:9',
  creation_type: 'novel',
})
const rules: FormRules = {
  name: [
    { required: true, message: '请输入项目名称', trigger: ['blur', 'input'] },
    {
      validator: (_rule, value: string) => value.trim().length >= 1 && value.trim().length <= 100,
      message: '项目名称需为 1-100 个字符',
      trigger: ['blur', 'input'],
    },
  ],
}

function openCreateModal() {
  formModel.value = { name: '', aspect_ratio: '16:9', creation_type: 'novel' }
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
  mutateCreate({
    name: formModel.value.name.trim(),
    aspect_ratio: formModel.value.aspect_ratio,
    creation_type: formModel.value.creation_type,
  })
}
</script>

<template>
  <section class="page project-list-page">
    <header class="page-header">
      <div>
        <h1 class="page-title">项目列表</h1>
        <p class="page-subtitle">管理你的漫剧创作项目</p>
      </div>
      <n-button type="primary" @click="openCreateModal">新建项目</n-button>
    </header>

    <n-spin :show="isPending">
      <div v-if="isError" class="state-block">
        <p class="state-text">加载失败：{{ errMsg(error) }}</p>
        <n-button secondary @click="() => refetch()">重试</n-button>
      </div>

      <div v-else-if="projects.length === 0" class="state-block">
        <n-empty description="还没有项目，创建一个开始你的漫剧创作吧">
          <template #extra>
            <n-button type="primary" @click="openCreateModal">新建项目</n-button>
          </template>
        </n-empty>
      </div>

      <div v-else class="project-grid">
        <article
          v-for="p in projects"
          :key="p.id"
          class="project-card"
          @click="goProject(p.id)"
        >
          <div class="cover" :class="`cover-${p.aspect_ratio.replace(':', '-')}`">
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
                @select="onDropdownSelect(p)"
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
    </n-spin>

    <n-modal
      :show="showModal"
      :on-update:show="onUpdateShow"
      preset="card"
      title="新建项目"
      class="create-modal"
      :style="{ width: '520px' }"
      :mask-closable="false"
    >
      <n-form
        ref="formRef"
        :model="formModel"
        :rules="rules"
        label-placement="top"
        :show-require-mark="false"
      >
        <n-form-item label="项目名称" path="name">
          <n-input
            v-model:value="formModel.name"
            placeholder="请输入项目名称（1-100 字符）"
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
      </n-form>
      <template #footer>
        <div class="modal-footer">
          <n-button @click="onUpdateShow(false)">取消</n-button>
          <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
        </div>
      </template>
    </n-modal>
  </section>
</template>

<style scoped>
.project-list-page {
  --bg-base: var(--bg-base);
  --bg-panel: var(--bg-panel);
  --bg-elevated: var(--bg-elevated);
  --border-subtle: var(--border-subtle);
  --border-strong: var(--border-strong);
  --brand-primary: var(--brand-primary);
  --brand-hover: var(--brand-hover);
  --danger: var(--danger);
  --text-primary: #e5e7eb;
  --text-secondary: #9ca3af;

  min-height: calc(100vh - var(--app-header-height, 56px));
  padding: 24px;
  max-width: 1280px;
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

.page-title {
  margin: 0;
  font-size: 20px;
  line-height: 28px;
  font-weight: 700;
}

.page-subtitle {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 20px;
  color: var(--text-secondary);
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
  grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
  gap: 16px;
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
}

.project-card:hover {
  border-color: var(--brand-primary);
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.15);
  transform: translateY(-2px);
}

.cover {
  position: relative;
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
}

.cover-9-16 {
  background: linear-gradient(135deg, #312e81 0%, var(--brand-primary) 100%);
}

.cover-16-9 {
  background: linear-gradient(135deg, #1e3a5f 0%, #3b82f6 100%);
}

.cover-1-1 {
  background: linear-gradient(135deg, #3f2d5b 0%, #8b5cf6 100%);
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
  font-size: 16px;
  line-height: 24px;
  font-weight: 500;
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
  font-size: 13px;
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
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
  min-width: 34px;
  text-align: right;
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
  background: rgba(99, 102, 241, 0.12);
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
  background: rgba(99, 102, 241, 0.12);
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
