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
  getEpisode,
  getSeries,
  getSeriesBible,
  listEpisodes,
  listPlotThreads,
  listSeriesAssets,
  listStoryEvents,
  saveSeriesBible,
} from '@/api/series'
import type { SeriesEpisode } from '@/types/series'

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
/** 续写按钮文案：尚无剧集时是「第 1 集」，否则「下一集」 */
const continueLabel = computed(() => (episodes.value.length === 0 ? '续写第 1 集' : '续写下一集'))
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

/** 剧集当前所处的下一步（用于列表按钮与引导文案，避免用户猜） */
type EpisodeStep = 'write' | 'analyzing' | 'review' | 'produce' | 'producing' | 'done'

function episodeStep(ep: SeriesEpisode): EpisodeStep {
  if (!ep.script_current_version_id) {
    return ep.script_status === 'analyzing' ? 'analyzing' : 'write'
  }
  if (ep.script_status === 'awaiting_review' || ep.script_status === 'draft') return 'review'
  if (ep.script_status === 'confirmed') {
    if (ep.production_status === 'completed') return 'done'
    if (ep.production_status === 'not_started') return 'produce'
    return 'producing'
  }
  return 'write'
}

const EPISODE_STEP_LABEL: Record<EpisodeStep, string> = {
  write: '下一步：续写生成剧本',
  analyzing: '剧本生成中',
  review: '下一步：审核并确认剧本',
  produce: '下一步：进入制作',
  producing: '制作进行中',
  done: '已完成',
}

/** 当前需要处理的第一集（按集号升序），没有则 null */
const focusEpisode = computed<SeriesEpisode | null>(
  () => episodes.value.find((ep) => episodeStep(ep) !== 'done') ?? null,
)

const hasBibleContent = computed(() => {
  const content = bible.value?.content
  return Boolean(content && Object.keys(content).length > 0)
})

const workflowSteps = computed(() => {
  const anyScript = episodes.value.some((ep) => Boolean(ep.script_current_version_id))
  const anyConfirmed = (series.value?.confirmed_script_count ?? 0) > 0
  const anyProducing = episodes.value.some((ep) => ep.production_status !== 'not_started')
  return [
    {
      title: '系列设定',
      description: hasBibleContent.value ? '设定已完善' : '内容为空，建议补充',
      // 创建系列时必定建立 Bible V1，设定内容不是续写的阻断项，故该步始终视为就绪
      status: 'finish',
    },
    {
      title: '续写生成剧本',
      description: anyScript ? '已有剧本草稿' : '填写要求，等待 AI 生成',
      status: anyScript ? 'finish' : 'process',
    },
    {
      title: '审核并确认',
      description: anyConfirmed ? '已确认正式剧情' : '确认后写入剧情事实',
      status: anyConfirmed ? 'finish' : anyScript ? 'process' : 'wait',
    },
    {
      title: '进入制作',
      description: anyProducing ? '已进入单集生产' : '解析、资产、分镜、视频、配音、成片',
      status: anyProducing ? 'finish' : anyConfirmed ? 'process' : 'wait',
    },
  ] as const
})

const nextAction = computed<{ label: string; hint: string; cta: string; run: () => void }>(() => {
  const ep = focusEpisode.value
  if (!ep) {
    return {
      label: continueLabel.value,
      hint: '填写本集创作要求后提交，系统会自动建立生产项目并生成剧本草稿。先在「系列设定」补充人物与世界观，生成会更稳定。',
      cta: continueLabel.value,
      run: () => goContinue(),
    }
  }
  const step = episodeStep(ep)
  const no = `第 ${ep.episode_number} 集`
  if (step === 'write') {
    return {
      label: `续写${no}剧本`,
      hint: '填写本集创作要求后提交，系统会自动建立生产项目并生成剧本草稿。',
      cta: `续写${no}`,
      run: () => goContinue(ep.id),
    }
  }
  if (step === 'analyzing') {
    return {
      label: `${no}剧本生成中`,
      hint: '后台任务正在生成剧本，可在任务中心查看进度，完成后回来审核。',
      cta: '查看任务进度',
      run: () => void router.push({ name: 'jobs' }),
    }
  }
  if (step === 'review') {
    return {
      label: `审核${no}剧本`,
      hint: '剧本草稿已生成，确认后才会写入正式剧情事实与连续性快照。',
      cta: `审核${no}`,
      run: () => goReview(ep.id),
    }
  }
  return {
    label: `进入${no}制作`,
    hint: '剧本已确认，进入单集工作台：剧本 → 解析 → 资产 → 分镜图 → 视频 → 配音 → 合片。',
    cta: `进入${no}制作`,
    run: () => void goWorkspace(ep),
  }
})

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

async function goWorkspace(ep: SeriesEpisode) {
  if (ep.project_id) {
    void router.push({ name: 'script', params: { id: ep.project_id } })
    return
  }
  // 列表可能来自缓存：进入制作前回源确认一次项目绑定，避免误报「尚未绑定生产项目」
  try {
    const fresh = await getEpisode(ep.id)
    if (fresh.project_id) {
      void queryClient.invalidateQueries({ queryKey: ['series-episodes', seriesId.value] })
      void router.push({ name: 'script', params: { id: fresh.project_id } })
      return
    }
    if (fresh.script_status === 'awaiting_review') {
      message.warning('剧本还在审核中：先确认剧本，再进入制作')
      return
    }
  } catch {
    /* 回源失败时按下方提示处理 */
  }
  message.warning('还差一步：先「续写本集」提交创作要求，系统会建立生产项目并生成剧本')
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
          <div class="head-main">
            <n-button text size="tiny" class="back-link" @click="router.push({ name: 'series-list' })">
              ← 全部系列
            </n-button>
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
            <!-- 持久工具栏动作，主操作交给下方「下一步」，避免同屏两个 primary 指向同一动作 -->
            <n-button secondary @click="goContinue()">{{ continueLabel }}</n-button>
            <n-button
              :title="`只创建占位剧集，不生成剧本；建议直接用「${continueLabel}」`"
              @click="createEpisodeMutation.mutate()"
              :loading="createEpisodeMutation.isPending.value"
            >
              仅新建空剧集
            </n-button>
          </n-space>
        </header>

        <section class="flow-panel">
          <div class="flow-rail" role="list" aria-label="创作流程">
            <div
              v-for="(step, idx) in workflowSteps"
              :key="idx"
              class="flow-step"
              :class="`is-${step.status}`"
              role="listitem"
            >
              <span class="flow-index">{{ String(idx + 1).padStart(2, '0') }}</span>
              <div class="flow-body">
                <span class="flow-title">{{ step.title }}</span>
                <span class="flow-desc">{{ step.description }}</span>
              </div>
            </div>
          </div>
          <p v-if="!hasBibleContent" class="bible-tip">
            系列设定内容为空（不阻断续写）：补充人物、世界观与风格后，续写与分镜一致性会明显更好。
            <n-button text size="tiny" type="primary" @click="openBibleEditor">现在补充</n-button>
          </p>
          <div class="next-action">
            <div class="next-text">
              <b class="next-label">下一步：{{ nextAction.label }}</b>
              <p class="hint">{{ nextAction.hint }}</p>
            </div>
            <n-button type="primary" @click="nextAction.run">{{ nextAction.cta }}</n-button>
          </div>
        </section>

        <section class="stat-row">
          <div class="stat"><b>{{ series.episode_count }}</b><span>剧集总数</span></div>
          <div class="stat"><b>{{ series.confirmed_script_count }}</b><span>已确认剧本</span></div>
          <div class="stat"><b>{{ series.completed_video_count }}</b><span>已完成视频</span></div>
          <div class="stat"><b>{{ series.open_plot_thread_count }}</b><span>未解决剧情线</span></div>
          <div class="stat"><b>v{{ series.bible_version }}</b><span>当前设定版本</span></div>
        </section>

        <n-tabs type="line" animated>
          <n-tab-pane name="episodes" tab="剧集列表">
            <n-empty v-if="episodes.length === 0" description="还没有剧集：填写本集创作要求后，系统会自动生成剧本并建立生产项目">
              <template #extra>
                <!-- 与上方「下一步」是同一个动作（goContinue），此处降为次级，避免同屏两个 primary -->
                <n-button secondary size="small" @click="goContinue()">续写第 1 集</n-button>
              </template>
            </n-empty>
            <div v-else class="episode-list">
              <div v-for="ep in episodes" :key="ep.id" class="episode-row">
                <span class="ep-no">{{ String(ep.episode_number).padStart(2, '0') }}</span>
                <div class="ep-main">
                  <div class="ep-title">
                    <span v-if="ep.title" class="ep-name">{{ ep.title }}</span>
                    <span v-else class="ep-name">第 {{ ep.episode_number }} 集</span>
                    <span class="ep-next">{{ EPISODE_STEP_LABEL[episodeStep(ep)] }}</span>
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
                  <n-button
                    v-if="episodeStep(ep) === 'analyzing'"
                    size="small"
                    @click="router.push({ name: 'jobs' })"
                  >生成中 · 查看进度</n-button>
                  <n-button
                    v-else-if="episodeStep(ep) === 'write'"
                    size="small"
                    type="primary"
                    @click="goContinue(ep.id)"
                  >续写本集</n-button>
                  <n-button
                    v-else-if="episodeStep(ep) === 'review'"
                    size="small"
                    type="primary"
                    @click="goReview(ep.id)"
                  >审核确认</n-button>
                  <n-button
                    v-else
                    size="small"
                    type="primary"
                    @click="goWorkspace(ep)"
                  >{{ ep.production_status === 'not_started' ? '进入制作' : '继续制作' }}</n-button>
                  <n-button
                    v-if="ep.script_status !== 'confirmed'"
                    size="small"
                    quaternary
                    @click="goContinue(ep.id)"
                  >重新续写</n-button>
                  <n-button v-else size="small" quaternary @click="goReview(ep.id)">查看剧本</n-button>
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
  max-width: 1180px;
  margin: 0 auto;
  padding: var(--sp-6) var(--sp-8) var(--sp-16);
}

/* 头部：标题用衬线，信息按栏线排布 */
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-6);
  flex-wrap: wrap;
}
.head-main {
  flex: 1 1 420px;
  min-width: 0;
}
.back-link {
  margin-bottom: var(--sp-2);
  color: var(--text-tertiary);
}
.back-link:hover {
  color: var(--brand-hover);
}
.detail-head h1 {
  margin: 0;
  font-family: var(--font-display);
  font-size: 32px;
  font-weight: 600;
  line-height: 1.2;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}
.sub {
  margin: var(--sp-2) 0 0;
  max-width: 62ch;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-secondary);
}
.meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-3);
  margin-top: var(--sp-3);
  font-size: 12px;
  color: var(--text-tertiary);
}
.meta > span + span {
  padding-left: var(--sp-3);
  border-left: 1px solid var(--border-subtle);
}

/* 创作流程：四段横向轨道，用栏线分隔；只有当前段被点亮 */
.flow-panel {
  margin-top: var(--sp-6);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
  overflow: hidden;
}
.flow-rail {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}
.flow-step {
  display: flex;
  gap: 10px;
  min-width: 0;
  padding: var(--sp-4) var(--sp-4) var(--sp-4) var(--sp-5);
  border-left: 1px solid var(--border-subtle);
  border-top: 2px solid transparent;
}
.flow-step:first-child {
  border-left: 0;
}
.flow-step.is-wait {
  opacity: 0.55;
}
.flow-step.is-process {
  border-top-color: var(--brand-primary);
}
.flow-index {
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 20px;
  color: var(--text-tertiary);
}
.flow-step.is-finish .flow-index {
  color: var(--brand-primary);
}
.flow-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.flow-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.flow-step.is-process .flow-title {
  color: var(--brand-hover);
}
.flow-desc {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
.bible-tip {
  margin: 0;
  padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-sunken);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.bible-tip .n-button {
  margin-left: 6px;
}

/* 下一步：全页唯一的强调块，左侧青瓷竖线 + 抬高底色 */
.next-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-5);
  padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--border-strong);
  border-left: 2px solid var(--brand-primary);
  background: var(--bg-elevated);
}
.next-text {
  min-width: 0;
}
.next-label {
  display: block;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.next-text .hint {
  margin: 4px 0 0;
  max-width: 62ch;
}
.hint {
  margin: 0 0 var(--sp-2);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

/* 统计：账本式单行，用竖线分栏，不做卡片 */
.stat-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  margin: var(--sp-6) 0 var(--sp-4);
  border-top: 1px solid var(--border-subtle);
  border-bottom: 1px solid var(--border-subtle);
}
.stat {
  padding: var(--sp-4) var(--sp-5);
  border-left: 1px solid var(--border-subtle);
}
.stat:first-child {
  padding-left: 0;
  border-left: 0;
}
.stat b {
  display: block;
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 500;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
}
.stat span {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-tertiary);
}

/* 剧集：连续账本行，分隔线组织层级，不再逐行套卡片 */
.episode-list {
  border-top: 1px solid var(--border-subtle);
}
.episode-row {
  display: flex;
  align-items: center;
  gap: var(--sp-4);
  flex-wrap: wrap;
  padding: var(--sp-4) var(--sp-2) var(--sp-4) 0;
  border-bottom: 1px solid var(--border-subtle);
  transition: background var(--transition-fast);
}
.episode-row:hover {
  background: var(--overlay-hover);
}
.ep-no {
  flex: 0 0 44px;
  align-self: flex-start;
  padding-top: 1px;
  padding-left: var(--sp-3);
  border-left: 2px solid transparent;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-tertiary);
  transition: color var(--transition-fast), border-color var(--transition-fast);
}
.episode-row:hover .ep-no {
  border-left-color: var(--brand-primary);
  color: var(--brand-hover);
}
.ep-main {
  flex: 1 1 260px;
  min-width: 200px;
}
.ep-title {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  flex-wrap: wrap;
  font-size: 14px;
}
.ep-name {
  font-weight: 600;
  color: var(--text-primary);
}
.ep-next {
  font-size: 12px;
  color: var(--brand-hover);
}
.ep-synopsis {
  display: -webkit-box;
  margin: 4px 0 0;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-tertiary);
}
.ep-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.ep-actions {
  display: flex;
  gap: 6px;
  margin-left: auto;
}

/* 其余标签页 */
.json-block {
  margin: 0;
  padding: var(--sp-3);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  background: var(--bg-sunken);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.cont-row {
  display: flex;
  gap: var(--sp-5);
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--text-secondary);
}
.mt12 {
  margin-top: var(--sp-3);
}
.ev-meta {
  font-size: 12px;
  color: var(--text-tertiary);
}
.thread-list,
.asset-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--sp-3);
}
.thread-head,
.asset-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-2);
}
.state-box {
  padding: var(--sp-10);
  text-align: center;
  color: var(--text-secondary);
}
.state-box.error {
  color: var(--danger);
}

@media (max-width: 768px) {
  .series-detail-page {
    padding: var(--sp-4) var(--sp-4) var(--sp-12);
  }
  .detail-head h1 {
    font-size: 26px;
  }
  .flow-rail {
    grid-template-columns: 1fr 1fr;
  }
  .flow-step:nth-child(3) {
    border-left: 0;
  }
  .flow-step:nth-child(n + 3) {
    border-top: 1px solid var(--border-subtle);
  }
  .stat-row {
    grid-template-columns: 1fr 1fr;
  }
  .stat {
    padding-left: var(--sp-4);
    border-left: 1px solid var(--border-subtle);
  }
  .stat:nth-child(odd) {
    padding-left: 0;
    border-left: 0;
  }
  .stat:nth-child(n + 3) {
    border-top: 1px solid var(--border-subtle);
  }
  .ep-actions {
    margin-left: 0;
  }
}
</style>
