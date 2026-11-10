<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  NButton,
  NCard,
  NEmpty,
  NInput,
  NModal,
  NSpace,
  NSpin,
  NTabPane,
  NTabs,
  NTag,
  NTimeline,
  NTimelineItem,
  useMessage,
} from 'naive-ui'
import {
  createEpisode,
  getContinuityOverview,
  getSeries,
  getSeriesBible,
  listEpisodes,
  listPlotThreads,
  listSeriesAssets,
  listStoryEvents,
  saveSeriesBible,
} from '@/api/series'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()
const seriesId = computed(() => String(route.params.id ?? ''))

const seriesQuery = useQuery({
  queryKey: ['series-detail', seriesId],
  queryFn: () => getSeries(seriesId.value),
  enabled: computed(() => Boolean(seriesId.value)),
})
const episodesQuery = useQuery({
  queryKey: ['series-episodes', seriesId],
  queryFn: () => listEpisodes(seriesId.value),
  enabled: computed(() => Boolean(seriesId.value)),
})
const bibleQuery = useQuery({
  queryKey: ['series-bible', seriesId],
  queryFn: () => getSeriesBible(seriesId.value),
  enabled: computed(() => Boolean(seriesId.value)),
})
const continuityQuery = useQuery({
  queryKey: ['series-continuity', seriesId],
  queryFn: () => getContinuityOverview(seriesId.value),
  enabled: computed(() => Boolean(seriesId.value)),
})
const threadsQuery = useQuery({
  queryKey: ['series-threads', seriesId],
  queryFn: () => listPlotThreads(seriesId.value),
  enabled: computed(() => Boolean(seriesId.value)),
})
const eventsQuery = useQuery({
  queryKey: ['series-events', seriesId],
  queryFn: () => listStoryEvents(seriesId.value, { limit: 100 }),
  enabled: computed(() => Boolean(seriesId.value)),
})
const assetsQuery = useQuery({
  queryKey: ['series-assets', seriesId],
  queryFn: () => listSeriesAssets(seriesId.value),
  enabled: computed(() => Boolean(seriesId.value)),
})

const series = computed(() => seriesQuery.data.value)
const episodes = computed(() => episodesQuery.data.value?.items ?? [])
const bible = computed(() => bibleQuery.data.value)
const continuity = computed(() => continuityQuery.data.value)
const threads = computed(() => threadsQuery.data.value ?? [])
const events = computed(() => eventsQuery.data.value ?? [])
const assets = computed(() => assetsQuery.data.value ?? [])

const SCRIPT_STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  analyzing: '分析中',
  awaiting_review: '待审核',
  confirmed: '已确认',
  superseded: '已被替代',
  rejected: '已驳回',
}
const PRODUCTION_STATUS_LABEL: Record<string, string> = {
  not_started: '未开始',
  queued: '排队中',
  running: '生产中',
  reviewing: '待审核',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}
const CONTINUITY_LABEL: Record<string, string> = {
  not_checked: '未检查',
  checking: '检查中',
  consistent: '一致',
  warning: '有警告',
  conflict: '有冲突',
  needs_review: '待人工确认',
}

const createEpisodeMutation = useMutation({
  mutationFn: () => createEpisode(seriesId.value, {}),
  onSuccess: (ep) => {
    message.success('已创建第 ' + ep.episode_number + ' 集')
    void queryClient.invalidateQueries({ queryKey: ['series-episodes'] })
    void queryClient.invalidateQueries({ queryKey: ['series-detail'] })
  },
  onError: (err: any) => message.error(err?.message ?? '创建剧集失败'),
})

const bibleModal = ref(false)
const bibleDraft = ref('')
const bibleSummary = ref('')

function openBibleEditor() {
  bibleDraft.value = JSON.stringify(bible.value?.content ?? {}, null, 2)
  bibleSummary.value = ''
  bibleModal.value = true
}

const saveBibleMutation = useMutation({
  mutationFn: (payload: { content: Record<string, unknown>; change_summary?: string | null }) =>
    saveSeriesBible(seriesId.value, payload),
  onSuccess: () => {
    message.success('已保存为新的设定版本，历史版本保留')
    bibleModal.value = false
    void queryClient.invalidateQueries({ queryKey: ['series-bible'] })
    void queryClient.invalidateQueries({ queryKey: ['series-detail'] })
  },
  onError: (err: any) => message.error(err?.message ?? '保存设定失败'),
})

function submitBible() {
  let parsed: Record<string, unknown>
  try {
    parsed = JSON.parse(bibleDraft.value || '{}')
  } catch {
    message.error('设定内容不是合法 JSON，请检查格式')
    return
  }
  saveBibleMutation.mutate({
    content: parsed,
    change_summary: bibleSummary.value.trim() || null,
  })
}

function goContinue(episodeId?: string) {
  void router.push({
    name: 'series-continue',
    params: { id: seriesId.value },
    query: episodeId ? { episodeId } : undefined,
  })
}

function goReview(episodeId: string) {
  void router.push({ name: 'episode-review', params: { id: episodeId } })
}

function goWorkspace(projectId: string | null) {
  if (!projectId) {
    message.warning('该剧集尚未绑定生产项目，先完成续写生成剧本')
    return
  }
  void router.push({ name: 'script', params: { id: projectId } })
}
</script>

<template>
  <div class="series-detail-page">
    <n-spin :show="seriesQuery.isFetching.value">
      <div v-if="seriesQuery.isError.value" class="state-box error">
        系列加载失败：{{ (seriesQuery.error.value as any)?.message }}
        <n-button size="small" @click="seriesQuery.refetch()">重试</n-button>
      </div>

      <template v-else-if="series">
        <header class="detail-head">
          <div>
            <h1>{{ series.name }}</h1>
            <p class="sub">{{ series.description || '暂无简介' }}</p>
            <div class="meta">
              <n-tag size="small" :type="series.status === 'active' ? 'success' : 'default'">
                {{ series.status === 'active' ? '进行中' : '已归档' }}
              </n-tag>
              <span v-if="series.genre">题材：{{ series.genre }}</span>
              <span v-if="series.style">风格：{{ series.style }}</span>
              <span v-if="series.target_audience">观众：{{ series.target_audience }}</span>
              <span>剧情状态版本 v{{ series.state_version }}</span>
            </div>
          </div>
          <n-space>
            <n-button type="primary" @click="goContinue()">续写下一集</n-button>
            <n-button @click="createEpisodeMutation.mutate()" :loading="createEpisodeMutation.isPending.value">
              仅新建空剧集
            </n-button>
          </n-space>
        </header>

        <section class="stat-row">
          <div class="stat"><b>{{ series.episode_count }}</b><span>剧集总数</span></div>
          <div class="stat"><b>{{ series.confirmed_script_count }}</b><span>已确认剧本</span></div>
          <div class="stat"><b>{{ series.completed_video_count }}</b><span>已完成视频</span></div>
          <div class="stat"><b>{{ series.open_plot_thread_count }}</b><span>未解决剧情线</span></div>
          <div class="stat"><b>v{{ series.bible_version }}</b><span>当前设定版本</span></div>
        </section>

        <n-tabs type="line" animated>
          <n-tab-pane name="episodes" tab="剧集列表">
            <n-empty v-if="episodes.length === 0" description="还没有剧集，点击右上角续写第一集" />
            <div v-else class="episode-list">
              <div v-for="ep in episodes" :key="ep.id" class="episode-row">
                <div class="ep-main">
                  <div class="ep-title">
                    第 {{ ep.episode_number }} 集
                    <span v-if="ep.title" class="ep-name">{{ ep.title }}</span>
                  </div>
                  <p class="ep-synopsis">{{ ep.synopsis || '暂无简介' }}</p>
                </div>
                <div class="ep-tags">
                  <n-tag size="small" :type="ep.script_status === 'confirmed' ? 'success' : 'warning'">
                    {{ SCRIPT_STATUS_LABEL[ep.script_status] ?? ep.script_status }}
                  </n-tag>
                  <n-tag size="small">{{ PRODUCTION_STATUS_LABEL[ep.production_status] ?? ep.production_status }}</n-tag>
                  <n-tag
                    size="small"
                    :type="ep.continuity_status === 'conflict' ? 'error' : ep.continuity_status === 'consistent' ? 'success' : 'default'"
                  >
                    连续性：{{ CONTINUITY_LABEL[ep.continuity_status] ?? ep.continuity_status }}
                  </n-tag>
                </div>
                <div class="ep-actions">
                  <n-button size="small" @click="goContinue(ep.id)">续写</n-button>
                  <n-button size="small" @click="goReview(ep.id)">审核</n-button>
                  <n-button size="small" quaternary @click="goWorkspace(ep.project_id)">进入制作</n-button>
                </div>
              </div>
            </div>
          </n-tab-pane>

          <n-tab-pane name="bible" tab="系列设定">
            <n-card size="small" :title="'当前激活版本 v' + (bible?.version ?? 0)">
              <template #header-extra>
                <n-button size="small" @click="openBibleEditor">编辑为新版本</n-button>
              </template>
              <p class="hint">修改设定会生成新版本，历史版本不会被覆盖。已确认的剧本不会被自动改写。</p>
              <pre class="json-block">{{ JSON.stringify(bible?.content ?? {}, null, 2) }}</pre>
            </n-card>
          </n-tab-pane>

          <n-tab-pane name="continuity" tab="连续性">
            <n-card size="small" title="剧情状态概览">
              <div class="cont-row">
                <span>当前状态版本：v{{ continuity?.state_version ?? 0 }}</span>
                <span>已确认事件：{{ continuity?.confirmed_event_count ?? 0 }}</span>
                <span>未解决剧情线：{{ continuity?.open_plot_threads ?? 0 }}</span>
                <span>存在冲突的剧集：{{ continuity?.episodes_with_conflict ?? 0 }}</span>
              </div>
              <p class="hint">
                最新快照版本：{{ continuity?.latest_snapshot ? 'v' + continuity.latest_snapshot.version : '暂无（尚未确认任何剧本）' }}
              </p>
            </n-card>
            <n-card size="small" title="已确认剧情事件" class="mt12">
              <n-empty v-if="events.length === 0" description="暂无已确认事件，确认第一集剧本后会写入" />
              <n-timeline v-else>
                <n-timeline-item
                  v-for="ev in events"
                  :key="ev.id"
                  :title="ev.title"
                  :content="ev.description || ''"
                  :type="ev.fact_status === 'confirmed' ? 'success' : 'warning'"
                >
                  <template #footer>
                    <span class="ev-meta">
                      {{ ev.event_type }} · 重要度 {{ ev.importance }} · {{ ev.fact_status }}
                    </span>
                  </template>
                </n-timeline-item>
              </n-timeline>
            </n-card>
          </n-tab-pane>

          <n-tab-pane name="threads" tab="剧情线">
            <n-empty v-if="threads.length === 0" description="暂无剧情线" />
            <div v-else class="thread-list">
              <n-card v-for="t in threads" :key="t.id" size="small" class="thread-card">
                <div class="thread-head">
                  <b>{{ t.title }}</b>
                  <n-tag size="small" :type="t.status === 'resolved' ? 'success' : 'warning'">
                    {{ t.status }}
                  </n-tag>
                </div>
                <p class="hint">{{ t.description || '暂无描述' }}</p>
                <p v-if="t.resolution_condition" class="hint">解决条件：{{ t.resolution_condition }}</p>
              </n-card>
            </div>
          </n-tab-pane>

          <n-tab-pane name="assets" tab="共享资产">
            <n-empty v-if="assets.length === 0" description="还没有登记系列共享资产" />
            <div v-else class="asset-list">
              <n-card v-for="a in assets" :key="a.id" size="small" class="asset-card">
                <div class="asset-head">
                  <b>{{ a.asset_name }}</b>
                  <n-tag size="small">{{ a.asset_type }}</n-tag>
                </div>
                <p class="hint">当前版本 v{{ a.current_version }} · Asset ID {{ a.asset_id }}</p>
              </n-card>
            </div>
          </n-tab-pane>
        </n-tabs>

        <n-modal
          v-model:show="bibleModal"
          preset="card"
          title="保存新的系列设定版本"
          style="width: 680px"
        >
          <n-space vertical>
            <n-input
              v-model:value="bibleSummary"
              placeholder="本次修改说明（可选，便于回溯）"
              maxlength="1000"
            />
            <n-input
              v-model:value="bibleDraft"
              type="textarea"
              :autosize="{ minRows: 12, maxRows: 22 }"
              placeholder="设定内容（JSON）"
            />
            <p class="hint">保存后会生成新版本并激活，旧版本保留可查。</p>
          </n-space>
          <template #footer>
            <n-space justify="end">
              <n-button @click="bibleModal = false">取消</n-button>
              <n-button type="primary" :loading="saveBibleMutation.isPending.value" @click="submitBible">
                保存为新版本
              </n-button>
            </n-space>
          </template>
        </n-modal>
      </template>
    </n-spin>
  </div>
</template>

<style scoped>
.series-detail-page {
  padding: 24px;
  max-width: 1100px;
  margin: 0 auto;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
.detail-head h1 {
  margin: 0 0 6px;
  font-size: 22px;
}
.sub {
  color: #666;
  margin: 0 0 8px;
  font-size: 13px;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: #888;
  align-items: center;
}
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  margin: 18px 0;
}
.stat {
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  padding: 12px;
  text-align: center;
}
.stat b {
  display: block;
  font-size: 20px;
}
.stat span {
  font-size: 12px;
  color: #888;
}
.episode-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.episode-row {
  border: 1px solid #eee;
  border-radius: 8px;
  padding: 12px;
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.ep-main {
  flex: 1 1 260px;
  min-width: 200px;
}
.ep-title {
  font-weight: 600;
  font-size: 14px;
}
.ep-name {
  font-weight: 400;
  color: #666;
  margin-left: 6px;
  font-size: 13px;
}
.ep-synopsis {
  margin: 4px 0 0;
  color: #888;
  font-size: 12px;
  line-height: 1.5;
}
.ep-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.ep-actions {
  display: flex;
  gap: 6px;
}
.hint {
  color: #888;
  font-size: 12px;
  line-height: 1.6;
  margin: 0 0 8px;
}
.json-block {
  background: #f6f6f6;
  padding: 12px;
  border-radius: 6px;
  font-size: 12px;
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.cont-row {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  font-size: 13px;
}
.mt12 {
  margin-top: 12px;
}
.ev-meta {
  font-size: 12px;
  color: #999;
}
.thread-list,
.asset-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.thread-head,
.asset-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.state-box {
  padding: 40px;
  text-align: center;
}
.state-box.error {
  color: #d03050;
}
</style>
