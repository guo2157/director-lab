<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { NButton, NEmpty, NInput, NPagination, NSelect, NSpin, NTag, useDialog, useMessage } from 'naive-ui'
import { archiveSeries, listSeries, unarchiveSeries } from '@/api/series'

const router = useRouter()
const dialog = useDialog()
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
  dialog.warning({
    title: '归档系列',
    content: '确认归档《' + name + '》？归档后不影响历史资产与已完成视频，但不能新建剧集。',
    positiveText: '归档',
    negativeText: '取消',
    onPositiveClick: () => archiveMutation.mutate(id),
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
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 20px;
}
.page-head h1 {
  font-size: 22px;
  margin: 0 0 6px;
}
.page-head p {
  margin: 0;
  color: #666;
  font-size: 13px;
  max-width: 720px;
  line-height: 1.6;
}
.filters {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
  flex-wrap: wrap;
}
.state-box {
  padding: 40px;
  text-align: center;
  color: #666;
}
.state-box.error {
  color: #d03050;
}
.series-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 16px;
}
.series-card {
  border: 1px solid #eee;
  border-radius: 10px;
  padding: 16px;
  cursor: pointer;
  transition: box-shadow 0.15s, border-color 0.15s;
  background: #fff;
}
.series-card:hover {
  border-color: #d0d0d0;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.card-head h2 {
  font-size: 16px;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.desc {
  color: #666;
  font-size: 13px;
  margin: 10px 0;
  min-height: 36px;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: #888;
  margin-bottom: 12px;
}
.stats span {
  background: #f6f6f6;
  padding: 2px 8px;
  border-radius: 10px;
}
.card-actions {
  display: flex;
  gap: 8px;
}
.pager {
  margin-top: 20px;
  display: flex;
  justify-content: center;
}
</style>
