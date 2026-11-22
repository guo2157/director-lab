<script setup lang="ts">
/**
 * 作品列表（首页）：头部统计与新建入口、配置引导、筛选排序、卡片网格。
 * 卡片 / 筛选条 / 新建弹窗已拆为子组件，本文件只保留页面编排与列表级操作。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NIcon, NInput, useMessage } from 'naive-ui'
import { AddOutline, SearchOutline } from '@vicons/ionicons5'
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
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorRetry from '@/components/ui/ErrorRetry.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import ProjectCard from '@/components/projects/ProjectCard.vue'
import { useProjectCovers } from '@/components/projects/useProjectCovers'
import ProjectFilterBar from '@/components/projects/ProjectFilterBar.vue'
import type { StatusFilter } from '@/components/projects/ProjectFilterBar.vue'
import CreateProjectModal from '@/components/projects/CreateProjectModal.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const router = useRouter()
const confirmDialog = useConfirmDialog()
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
/**
 * 项目封面：从各项目已有资产里挑一张图（keyframe > scene > character）。
 * 未解析/未出图的项目拿不到封面，卡片自动回退到占位封面，不伪造图片。
 */
const projectCovers = useProjectCovers(computed(() => projects.value.map((p) => p.id)))
function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败，请稍后重试'
}

/* ---------- 搜索 / 筛选 ----------
 * 排序轴已移除（2026-11-19）：只有「最近更新 / 名称」两个选项、默认就是最近更新，
 * 用户几乎不会切；而它占着工具栏右侧一整个分段控件的宽度。固定按 updated_at 倒序，
 * 与「最近更新」是同一结果，功能无损失。 */
const searchKeyword = ref('')
const statusFilter = ref<StatusFilter>('all')
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
  // 固定按更新时间倒序（原「最近更新」选项，现为唯一顺序）
  return [...list].sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
})
/* 页头元信息与 isFiltering 已移除（2026-11-19）：
   - 「共 N 个作品」与筛选 chip 上的「全部 N」是同一个数，说两遍不提供新信息，
     规模感由 chips 的计数徽标承担；
   - 原来的 isFiltering 只服务于那句被删掉的副标题，空态判断走
     `filteredProjects.length === 0`，不需要额外布尔。 */
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
  mutationFn: (vars: { id: string; force?: boolean }) =>
    deleteProject(vars.id, { force: vars.force }),
  onSuccess: () => {
    message.success('作品已删除')
    invalidateProjects()
  },
  onError: (e) => message.error(errMsg(e)),
})
const { mutate: mutatePurge } = useMutation({
  mutationFn: (vars: { id: string; force?: boolean }) =>
    purgeProject(vars.id, { force: vars.force }),
  onSuccess: (res) => {
    const rows = Object.values(res?.deleted ?? {}).reduce((a, b) => a + b, 0)
    const files = res?.media?.removed ?? 0
    // 连带取消的任务数必须有回执：用户点删除时未必记得还有任务在跑
    const cancelled = res?.cancelled?.jobs ?? 0
    const head = cancelled
      ? `作品已彻底删除：取消 ${cancelled} 个在途任务、清理 ${rows} 条记录、${files} 个媒体文件`
      : `作品已彻底删除：清理 ${rows} 条记录、${files} 个媒体文件`
    message.success(head)
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
    void confirmDelete(p)
  } else if (key === 'purge') {
    void confirmPurge(p)
  }
}

/** 读取影响面预览：失败返回 null（不阻断删除，后端会在 force=true 时兜住取消逻辑） */
async function loadPreview(p: Project): Promise<PurgePreview | null> {
  try {
    return await getPurgePreview(p.id)
  } catch {
    return null
  }
}

async function confirmDelete(p: Project) {
  const preview = await loadPreview(p)
  const blocking = preview?.blocking_jobs ?? 0
  // 任务页入口已下线，「请先去任务页取消」是无法执行的指引：
  // 这里改为如实告知 + 用户确认后由服务端自动取消在途任务再删除。
  confirmDialog({
    title: '删除作品',
    message: blocking
      ? `作品「${p.name}」还有 ${blocking} 个进行中的任务。继续删除会先把这些任务自动取消，再删除作品（不可恢复）。`
      : `确定要删除作品「${p.name}」吗？删除后不可恢复。`,
    impact: [
      `作品「${p.name}」`,
      ...(blocking ? [`进行中的任务 ${blocking} 个（将被自动取消）`] : []),
    ],
    confirmText: '删除',
    onConfirm: () => {
      mutateDelete({ id: p.id, force: true })
    },
  })
}

async function confirmPurge(p: Project) {
  const preview = await loadPreview(p)
  if (!preview) {
    message.error('读取删除影响面失败，请稍后重试')
    return
  }
  const c = preview.counts
  const impact = [
    `场景 ${c.scenes} 个、镜头 ${c.shots} 个`,
    `资产 ${c.assets} 个（含分镜图、角色图、视频等媒体文件）`,
    `任务记录 ${c.jobs} 条`,
    `角色 ${c.characters} 个、道具 ${c.props} 个`,
    `剧本版本 ${c.script_versions} 个`,
    ...(preview.blocking_jobs
      ? [`进行中的任务 ${preview.blocking_jobs} 个（将被自动取消）`]
      : []),
  ]
  if (!preview.deletable) {
    confirmDialog({
      title: '作品还有任务在执行',
      message: `作品「${p.name}」还有 ${preview.blocking_jobs} 个进行中的任务（排队 / 运行 / 提交待确认 / 链式等待）。继续将先自动取消这些任务，再彻底删除作品及其全部内容，不可恢复。`,
      impact,
      confirmText: '取消任务并彻底删除',
      onConfirm: () => {
        mutatePurge({ id: p.id, force: true })
      },
    })
    return
  }
  confirmDialog({
    title: '彻底删除作品（不可恢复）',
    message: `即将永久删除作品「${p.name}」及其全部内容，数据库记录与磁盘上的媒体文件都会被清除，此操作无法撤销。`,
    impact,
    confirmText: '确认彻底删除',
    onConfirm: () => {
      mutatePurge({ id: p.id, force: true })
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
    <!-- 页标题不再重复顶栏已高亮的「作品」：顶栏胶囊就是本页的身份。
         H1 语义保留给读屏与文档大纲，视觉上不占位。 -->
    <h1 class="visually-hidden">作品</h1>

    <div v-if="missingConfigs.length" class="config-banner">
      <span class="banner-text">
        尚未完成 {{ missingConfigs.join('、') }} 配置，创建作品前请先完成配置
      </span>
      <n-button size="small" class="btn-brand" @click="router.push('/settings')">前往设置</n-button>
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
        <!-- 单行工具栏（2026-11-19）：状态筛选 + 搜索 + 新建并排。
             左端筛选是「看哪些」，右端搜索/新建是「找」和「造」，两组语义不同，
             中间靠 flex 弹性留白分隔，不需要额外分隔线。
             顺序固定：筛选（主轴，品牌色）→ 弹性留白 → 搜索 → 主 CTA。 -->
        <div class="list-toolbar">
          <ProjectFilterBar v-model:status-filter="statusFilter" :counts="counts" />

          <n-input
            v-model:value="searchKeyword"
            placeholder="搜索作品"
            clearable
            class="search-input"
            size="medium"
          >
            <template #prefix>
              <n-icon :component="SearchOutline" :size="15" class="search-mark" />
            </template>
          </n-input>

          <n-button class="btn-brand new-btn" @click="openCreateModal">
            <n-icon :component="AddOutline" :size="16" class="new-plus" />
            <span class="new-text">新建作品</span>
          </n-button>
        </div>

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
          <ProjectCard
            v-for="(p, idx) in filteredProjects"
            :key="p.id"
            :project="p"
            :index="idx"
            :cover-url="projectCovers[p.id] ?? ''"
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
  min-height: calc(100vh - var(--app-header-height, 64px));
  padding: var(--sp-6) var(--sp-6) var(--sp-16);
  background: var(--bg-base);
  color: var(--text-primary);
}

/* ---------- 单行工具栏 ----------
   三行压成一行：状态筛选 + 搜索 + 新建并排。
   左侧筛选是「看哪些」（主轴，品牌色），右侧搜索/新建是「找」和「造」，
   中间用 flex: 1 的留白隔开，不画分隔线——分隔线会多出一条视觉噪音。

   高度：所有控件锁 36px，与顶栏导航项同高，页面顶部形成一条统一的
   控件带（原来搜索框 34px + chips 30px 高低不齐）。 */
.list-toolbar {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
  margin: 0 0 var(--sp-5);
}
.list-toolbar > :first-child {
  min-width: 0;
}
/* 弹性留白：把搜索/新建推到右端，不占任何可见面积 */
.list-toolbar::after {
  content: '';
  flex: 1 1 0;
  order: 1;
  min-width: var(--sp-2);
}
.search-input {
  order: 2;
  width: 220px;
  flex: 0 0 auto;
}
.new-btn {
  order: 3;
  height: 36px;
  padding: 0 16px;
  flex: 0 0 auto;
}
/* gap 必须加在 .n-button__content 上，不能加在 .new-btn：
   Naive 把按钮内容包在 .n-button__content 里，外层 flex 的 gap 作用不到
   图标与文字之间（实测 gap 声明为 6px 但实际间距 0，加号紧贴文字）。 */
.new-btn :deep(.n-button__content) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.new-plus {
  flex: none;
}
.new-text {
  white-space: nowrap;
}
.search-mark {
  display: flex;
  color: var(--text-tertiary);
}

/* ---------- 配置横幅 ---------- */
.config-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-5);
  border: 1px solid var(--warning-line);
  background: var(--warning-soft);
  border-radius: var(--radius-md);
}
.banner-text {
  font-size: 13px;
  color: var(--text-primary);
}

/* ---------- 栅格 ---------- */
/* 骨架与正式栅格同宽同列，避免加载完成时跳变 */
.grid-skeleton {
  grid-template-columns: repeat(auto-fill, minmax(min(250px, 100%), 1fr)) !important;
}
/* 列宽下限 250px：封面改为 3:4 后必须重新算列数。
   实测内容区宽 1392（1440 视口 − 左栏已由 .content-body 消化），
   250px 下限 → 5 列，卡宽 266、封面 355、卡高约 485，
   1440×900 下一屏可见 1.5 行（改前 4 列卡宽 336、封面 251 横版、卡高 381，
   一屏可见 1.8 行但封面是被横裁的竖图）。
   项目数不是 5 的倍数时最后一行会有空位 —— 这是数据量，不是布局缺陷；
   为了让 4 个项目刚好铺满而把卡宽拉到 336，封面会涨到 448，
   单卡 578 反而一屏只看得到一行。 */
.project-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(250px, 100%), 1fr));
  gap: var(--sp-4);
  align-items: stretch;
}

@media (max-width: 768px) {
  .project-list-page {
    padding: var(--sp-4) var(--sp-4) var(--sp-12);
  }
  /* 窄屏工具栏：筛选 chips 独占第一行（4 个 chip + 计数约 300px，
     和搜索框挤一行会把搜索压到不可用），搜索 + 新建第二行。
     chips 自身在窄屏改为单行横向滚动（见 ProjectFilterBar 内的媒体查询）。 */
  .list-toolbar {
    gap: var(--sp-2);
  }
  .list-toolbar::after {
    display: none;
  }
  .list-toolbar > :first-child {
    order: 0;
    flex: 1 1 100%;
    min-width: 0;
  }
  .search-input {
    order: 2;
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
  }
  .new-btn {
    order: 3;
  }
  /* 窄屏必须双列，不能单列：封面改 3:4 后，390 视口单列卡宽 358 → 封面 477px，
     加信息区单卡约 610px，一屏只看得到 1 张 —— 列表页失去「横向比较」这个
     唯一的存在理由（实测改前单列时首屏只有 1.2 张卡）。
     双列后卡宽约 165、封面 220、单卡约 330，一屏可见 2.5 张。 */
  .project-grid,
  .grid-skeleton {
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    gap: var(--sp-3);
  }
}
</style>
