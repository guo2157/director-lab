<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NEmpty, NInput, NPagination, NSelect, NSpin, NTag, useMessage } from 'naive-ui'
import { archiveSeries, listSeries, unarchiveSeries } from '@/api/series'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { relativeTime } from '@/utils/time'

const router = useRouter()
const confirmDialog = useConfirmDialog()
const message = useMessage()
const queryClient = useQueryClient()

const keyword = ref('')
const statusFilter = ref<string>('')
const page = ref(1)
const pageSize = 12

const STATUS_OPTIONS = [
  { label: '全部状态', value: '' },
  { label: '进行中', value: 'active' },
  { label: '已归档', value: 'archived' },
]

const queryParams = computed(() => ({
  keyword: keyword.value || undefined,
  status: statusFilter.value || undefined,
  page: page.value,
  page_size: pageSize,
}))

const { data, isPending, isError, error, refetch, isFetching } = useQuery({
  queryKey: ['series-list', queryParams],
  queryFn: () => listSeries(queryParams.value),
})

const items = computed(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)

function openSeries(id: string) {
  void router.push({ name: 'series-detail', params: { id } })
}

function goCreate() {
  void router.push({ name: 'series-create' })
}

const archiveMutation = useMutation({
  mutationFn: (id: string) => archiveSeries(id),
  onSuccess: () => {
    message.success('系列已归档，历史资产与生产记录均保留')
    void queryClient.invalidateQueries({ queryKey: ['series-list'] })
  },
  onError: (err: any) => message.error(err?.message ?? '归档失败'),
})

const unarchiveMutation = useMutation({
  mutationFn: (id: string) => unarchiveSeries(id),
  onSuccess: () => {
    message.success('系列已恢复')
    void queryClient.invalidateQueries({ queryKey: ['series-list'] })
  },
  onError: (err: any) => message.error(err?.message ?? '恢复失败'),
})

function onArchive(id: string, name: string) {
  confirmDialog({
    title: '归档系列',
    message: `确认归档《${name}》？归档后不影响历史资产与已完成视频，但不能新建剧集。`,
    impact: [`系列《${name}》`],
    confirmText: '归档',
    onConfirm: () => archiveMutation.mutate(id),
  })
}

function onSearch() {
  page.value = 1
  void refetch()
}
</script>

<template>
  <div class="series-list-page">
    <header class="page-head">
      <div class="head-text">
        <h1>系列管理</h1>
        <p>管理长篇系列的设定、剧集、剧情事实与连续性状态；单集生产流程仍沿用原有项目工作台。</p>
      </div>
      <n-button type="primary" @click="goCreate">新建系列</n-button>
    </header>

    <div class="filters">
      <n-input
        v-model:value="keyword"
        placeholder="搜索系列名称"
        clearable
        style="max-width: 280px"
        @keyup.enter="onSearch"
      />
      <n-select v-model:value="statusFilter" :options="STATUS_OPTIONS" style="width: 160px" @update:value="onSearch" />
      <n-button @click="onSearch">搜索</n-button>
    </div>

    <n-spin :show="isFetching">
      <div v-if="isPending" class="state-box">加载中…</div>

      <div v-else-if="isError" class="state-box error">
        <p>系列列表加载失败：{{ (error as any)?.message ?? '未知错误' }}</p>
        <n-button size="small" @click="refetch()">重试</n-button>
      </div>

      <n-empty v-else-if="items.length === 0" description="还没有系列，先创建一个系列来管理长篇创作">
        <template #extra>
          <n-button type="primary" size="small" @click="goCreate">新建系列</n-button>
        </template>
      </n-empty>

      <div v-else class="series-grid">
        <article v-for="item in items" :key="item.id" class="series-card" @click="openSeries(item.id)">
          <div class="card-head">
            <h2>{{ item.name }}</h2>
            <n-tag size="small" :type="item.status === 'active' ? 'success' : 'default'">
              {{ item.status === 'active' ? '进行中' : '已归档' }}
            </n-tag>
          </div>
          <p class="desc">{{ item.description || '暂无简介' }}</p>
          <div class="stats">
            <span>剧集 {{ item.episode_count }}</span>
            <span>已确认剧本 {{ item.confirmed_script_count }}</span>
            <span>已完成视频 {{ item.completed_video_count }}</span>
            <span>未解决剧情线 {{ item.open_plot_thread_count }}</span>
            <span>设定 v{{ item.bible_version }}</span>
          </div>
          <p class="updated" :title="new Date(item.updated_at).toLocaleString('zh-CN')">
            更新于 {{ relativeTime(item.updated_at) }}
          </p>
          <div class="card-actions" @click.stop>
            <n-button size="small" @click="openSeries(item.id)">进入</n-button>
            <n-button
              v-if="item.status === 'active'"
              size="small"
              quaternary
              @click="onArchive(item.id, item.name)"
            >归档</n-button>
            <n-button
              v-else
              size="small"
              quaternary
              :loading="unarchiveMutation.isPending.value"
              @click="unarchiveMutation.mutate(item.id)"
            >恢复</n-button>
          </div>
        </article>
      </div>

      <div v-if="total > pageSize" class="pager">
        <n-pagination v-model:page="page" :page-size="pageSize" :item-count="total" @update:page="refetch()" />
      </div>
    </n-spin>
  </div>
</template>

<style scoped>
.series-list-page {
  max-width: var(--page-max);
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-8) var(--sp-16);
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-5);
  flex-wrap: wrap;
  padding-bottom: var(--sp-4);
  margin-bottom: var(--sp-5);
  border-bottom: 1px solid var(--border-subtle);
}
.head-text {
  flex: 1 1 420px;
  min-width: 0;
}
.page-head h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 30px;
  font-weight: 600;
  line-height: 1.25;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}
.page-head p {
  margin: var(--sp-2) 0 0;
  max-width: 68ch;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.filters {
  display: flex;
  gap: var(--sp-3);
  margin-bottom: var(--sp-5);
  flex-wrap: wrap;
}
.state-box {
  padding: var(--sp-10);
  text-align: center;
  color: var(--text-secondary);
}
.state-box.error {
  color: var(--danger);
}
.series-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--sp-3);
}
/* 系列卡：像片盒一样以边框和左侧细线区分，悬停只换边线颜色，不加阴影 */
.series-card {
  display: flex;
  flex-direction: column;
  padding: var(--sp-4) var(--sp-4) var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}
.series-card:hover {
  background: var(--bg-elevated);
  border-color: var(--border-strong);
  border-left-color: var(--brand-primary);
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-2);
}
.card-head h2 {
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 17px;
  font-weight: 600;
  color: var(--text-primary);
}
.desc {
  display: -webkit-box;
  min-height: 36px;
  margin: var(--sp-2) 0 var(--sp-3);
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  font-size: 13px;
  line-height: 1.55;
  color: var(--text-secondary);
}
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-1);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
}
.updated {
  margin: 0 0 var(--sp-3);
  font-size: 11px;
  color: var(--text-tertiary);
}
.card-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: auto;
}
.pager {
  display: flex;
  justify-content: center;
  margin-top: var(--sp-5);
}

@media (max-width: 768px) {
  .series-list-page {
    padding: var(--sp-4) var(--sp-4) var(--sp-12);
  }
  .page-head h1 {
    font-size: 24px;
  }
  .series-grid {
    grid-template-columns: 1fr;
  }
}
</style>
