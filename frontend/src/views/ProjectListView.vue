<script setup lang="ts">
/**
 * 作品列表（首页）：头部统计与新建入口、配置引导、筛选排序、卡片网格。
 * 卡片 / 筛选条 / 新建弹窗已拆为子组件，本文件只保留页面编排与列表级操作。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NInput, useDialog, useMessage } from 'naive-ui'
import {
  archiveProject,
  deleteProject,
  getPurgePreview,
  listProjects,
  purgeProject,
} from '@/api/projects'
import type { PurgePreview } from '@/api/projects'
import { getSettings, listComfyuiInstances } from '@/api/settings'
import type { Project } from '@/types/project'
import type { SeriesListItem } from '@/types/series'
import PageHeader from '@/components/ui/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import ProjectCard from '@/components/projects/ProjectCard.vue'
import ProjectFilterBar from '@/components/projects/ProjectFilterBar.vue'
import type { SortKey, StatusFilter } from '@/components/projects/ProjectFilterBar.vue'
import CreateProjectModal from '@/components/projects/CreateProjectModal.vue'

const router = useRouter()
const dialog = useDialog()
const message = useMessage()
const queryClient = useQueryClient()

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
const statusFilter = ref<StatusFilter>('all')
const sortKey = ref<SortKey>('updated')
const counts = computed<Record<StatusFilter, number>>(() => ({
  all: projects.value.length,
  draft: projects.value.filter((p) => p.status === 'draft').length,
  active: projects.value.filter((p) => p.status === 'active').length,
  archived: projects.value.filter((p) => p.status === 'archived').length,
}))
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
const activeCount = computed(() => counts.value.active)
const isFiltering = computed(() => searchKeyword.value.trim() !== '' || statusFilter.value !== 'all')
const headerSubtitle = computed(() => {
  if (!projects.value.length) return '从剧本到成片，一集一集往下走'
  if (isFiltering.value) return `筛选出 ${filteredProjects.value.length} / ${projects.value.length} 个作品`
  const done = counts.value.archived
  return `共 ${projects.value.length} 个作品 · ${activeCount.value} 个进行中 · ${done} 个已归档`
})

/* ---------- 列表级操作 ---------- */
function invalidateProjects() {
  void queryClient.invalidateQueries({ queryKey: ['projects'] })
}
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

function goProject(id: string) {
  void router.push(`/project/${id}/script`)
}
function onCardAction(key: string, p: Project) {
  if (key === 'archive') {
    mutateArchive(p.id)
  } else if (key === 'delete') {
    confirmDelete(p)
  } else if (key === 'purge') {
    void confirmPurge(p)
  }
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

/* ---------- 新建弹窗 ---------- */
const createModalRef = ref<InstanceType<typeof CreateProjectModal> | null>(null)
function openCreateModal() {
  createModalRef.value?.open()
}
function onSeriesCreated(s: SeriesListItem) {
  void router.push({ name: 'series-detail', params: { id: s.id } })
}
function clearFilters() {
  searchKeyword.value = ''
  statusFilter.value = 'all'
}
</script>

<template>
  <section class="page project-list-page">
    <PageHeader title="作品" :subtitle="headerSubtitle">
      <template #actions>
        <n-input
          v-model:value="searchKeyword"
          placeholder="搜索作品"
          clearable
          class="search-input"
          size="small"
        />
        <n-button type="primary" @click="openCreateModal">新建作品</n-button>
      </template>
    </PageHeader>

    <div v-if="missingConfigs.length" class="config-banner">
      <span class="banner-text">
        尚未完成 {{ missingConfigs.join('、') }} 配置，创建作品前请先完成配置
      </span>
      <n-button size="small" type="primary" @click="router.push('/settings')">前往设置</n-button>
    </div>

    <!-- 错误 -->
    <ErrorRetry
      v-if="isError"
      title="作品列表加载失败"
      :message="errMsg(error)"
      @retry="refetch()"
    />

    <!-- 首次加载：卡片骨架，保持栅格形状避免布局跳动 -->
    <SkeletonList v-else-if="isPending" class="grid-skeleton" :rows="8" :columns="4" height="240px" />

    <template v-else>
      <!-- 空：一个作品都没有 -->
      <EmptyState
        v-if="projects.length === 0"
        title="还没有作品"
        description="新建一个作品，从剧本、分镜到成片走一遍完整流程。"
        action-text="新建作品"
        @action="openCreateModal"
      />

      <template v-else>
        <ProjectFilterBar
          v-model:status-filter="statusFilter"
          v-model:sort-key="sortKey"
          :counts="counts"
        />

        <!-- 空：筛选后无匹配 -->
        <EmptyState
          v-if="filteredProjects.length === 0"
          compact
          title="没有匹配的作品"
          description="换个关键词，或把筛选条件放宽一些。"
          action-text="清除筛选"
          @action="clearFilters"
        />

        <div v-else class="project-grid">
          <button type="button" class="launch-card" @click="openCreateModal">
            <span class="launch-plus" aria-hidden="true">+</span>
            <span class="launch-text">新建作品</span>
            <span class="launch-sub">开始新的漫剧创作</span>
          </button>
          <ProjectCard
            v-for="(p, idx) in filteredProjects"
            :key="p.id"
            :project="p"
            :index="idx"
            @open="goProject"
            @action="onCardAction"
          />
        </div>
      </template>
    </template>

    <CreateProjectModal
      ref="createModalRef"
      @created="invalidateProjects"
      @created-series="onSeriesCreated"
    />
  </section>
</template>

<style scoped>
.project-list-page {
  min-height: calc(100vh - var(--app-header-height, 52px));
  padding: var(--sp-6);
  max-width: 1440px;
  margin: 0 auto;
  background: var(--bg-base);
  color: var(--text-primary);
}
.page-header {
  margin: 4px 0 var(--sp-5);
}
.search-input {
  width: 240px;
}
.config-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-5);
  border: 1px solid var(--warning-line);
  background: var(--warning-soft);
  border-radius: var(--radius-lg);
}
.banner-text {
  font-size: 13px;
  color: var(--text-primary);
}
/* 骨架与正式栅格同宽同列，避免加载完成时跳变 */
.grid-skeleton {
  grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr)) !important;
}
.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
  gap: var(--sp-4);
}
.launch-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 240px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-xl);
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
  background: var(--brand-soft);
  color: var(--brand-primary);
}
.launch-card:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
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
</style>

<style>
/* 删除确认弹窗：确认按钮使用危险色 */
.n-dialog.danger-dialog .n-button--primary-type {
  --n-color: var(--danger) !important;
  --n-color-hover: var(--state-danger) !important;
  --n-color-pressed: var(--state-danger) !important;
  --n-color-focus: var(--state-danger) !important;
  --n-text-color: var(--text-primary) !important;
  --n-text-color-hover: var(--text-primary) !important;
  --n-text-color-pressed: var(--text-primary) !important;
  --n-text-color-focus: var(--text-primary) !important;
  --n-border: 1px solid transparent !important;
  --n-border-hover: 1px solid transparent !important;
  --n-border-pressed: 1px solid transparent !important;
  --n-border-focus: 1px solid transparent !important;
}
</style>
