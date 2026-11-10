<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NCard, NCheckbox, NEmpty, NInput, NSpace, NSpin, NTag, useMessage } from 'naive-ui'
import {
  checkContinuity,
  confirmScript,
  getEpisode,
  getSeries,
  listContinuityIssues,
  listScripts,
} from '@/api/series'
import { ApiError } from '@/api/client'
import type { SeriesEpisode, ScriptConfirmResult, SeriesScript } from '@/types/series'

/**
 * 剧集审核页：确认剧本并提交正式剧情状态。
 * 与既有 EpisodeContinuity（视频首尾帧连续性）无关，本页只处理系列剧情连续性。
 * 确认动作必须携带 expected_series_state_version 做乐观锁，冲突时不覆盖他人结果。
 */
const route = useRoute()
const router = useRouter()
const message = useMessage()

const episodeId = computed(() => String(route.params.id ?? ''))

const loading = ref(true)
const loadError = ref('')
const episode = ref<SeriesEpisode | null>(null)
const seriesStateVersion = ref<number | null>(null)
const scripts = ref<SeriesScript[]>([])
const selectedScriptId = ref('')

const issues = ref<Array<Record<string, unknown>>>([])
const issuesStatus = ref('')
const checking = ref(false)
const checkError = ref('')

const reviewNote = ref('')
const ignoreWarnings = ref(false)
const confirming = ref(false)
const confirmError = ref('')
const confirmResult = ref<ScriptConfirmResult | null>(null)

const selectedScript = computed(() => scripts.value.find((s) => s.id === selectedScriptId.value) ?? null)
const canConfirm = computed(() => {
  const s = selectedScript.value
  if (!s) return false
  return ['draft', 'awaiting_review', 'analyzing', 'rejected'].includes(s.status)
})
const conflictCount = computed(
  () => issues.value.filter((i) => String(i.severity ?? '').toLowerCase() === 'conflict').length,
)
const warningCount = computed(
  () => issues.value.filter((i) => String(i.severity ?? '').toLowerCase() === 'warning').length,
)

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

const SEVERITY_LABEL: Record<string, string> = {
  conflict: '冲突',
  warning: '警告',
  info: '提示',
}

function label(map: Record<string, string>, key: string | null | undefined): string {
  if (!key) return '-'
  return map[key] ?? key
}

function severityTagType(sev: string): 'error' | 'warning' | 'info' | 'default' {
  const s = sev.toLowerCase()
  if (s === 'conflict') return 'error'
  if (s === 'warning') return 'warning'
  if (s === 'info') return 'info'
  return 'default'
}

function formatEventIds(value: unknown): string {
  if (Array.isArray(value)) return value.join('、')
  if (typeof value === 'string' && value) return value
  return ''
}

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    const ep = await getEpisode(episodeId.value)
    episode.value = ep
    if (ep.project_id) {
      // 系列状态版本用于乐观锁，仅在提交确认时必须
    }
    const series = await getSeries(ep.series_id)
    seriesStateVersion.value = series.state_version
    scripts.value = await listScripts(episodeId.value)
    if (scripts.value.length) {
      const preferred =
        scripts.value.find((s) => s.id === ep.script_current_version_id) ??
        scripts.value.find((s) => s.status === 'awaiting_review') ??
        scripts.value[0]
      selectedScriptId.value = preferred.id
    }
    await loadIssues()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '审核信息加载失败'
  } finally {
    loading.value = false
  }
}

async function loadIssues() {
  try {
    const res = await listContinuityIssues(episodeId.value)
    issues.value = res.issues ?? []
    issuesStatus.value = res.status ?? ''
  } catch (e) {
    checkError.value = e instanceof Error ? e.message : '连续性问题加载失败'
  }
}

async function runCheck() {
  checking.value = true
  checkError.value = ''
  try {
    await checkContinuity(episodeId.value)
    await loadIssues()
    if (episode.value) {
      const ep = await getEpisode(episodeId.value)
      episode.value = ep
    }
    message.success('连续性检查完成')
  } catch (e) {
    checkError.value = e instanceof Error ? e.message : '连续性检查失败'
  } finally {
    checking.value = false
  }
}

async function refreshSeriesVersion() {
  if (!episode.value) return
  const series = await getSeries(episode.value.series_id)
  seriesStateVersion.value = series.state_version
}

async function doConfirm() {
  const script = selectedScript.value
  if (!script) return
  if (seriesStateVersion.value === null) {
    message.warning('系列状态版本未知，请刷新页面后重试')
    return
  }
  confirming.value = true
  confirmError.value = ''
  try {
    confirmResult.value = await confirmScript(episodeId.value, script.id, {
      expected_series_state_version: seriesStateVersion.value,
      review_note: reviewNote.value.trim() || null,
      ignore_warnings: ignoreWarnings.value,
    })
    message.success('剧本已确认，剧情状态已提交')
    const ep = await getEpisode(episodeId.value)
    episode.value = ep
    scripts.value = await listScripts(episodeId.value)
    seriesStateVersion.value = confirmResult.value.series_state_version
    await loadIssues()
  } catch (e) {
    if (e instanceof ApiError && (e.status === 409 || e.code.includes('conflict') || e.code.includes('version'))) {
      confirmError.value = `系列状态已变化，本次确认未生效：${e.message}。请刷新后重新确认。`
      try {
        await refreshSeriesVersion()
      } catch {
        /* 忽略刷新失败，保留原错误提示 */
      }
    } else {
      confirmError.value = e instanceof Error ? e.message : '剧本确认失败'
    }
  } finally {
    confirming.value = false
  }
}

function goBack() {
  if (episode.value) {
    void router.push({ name: 'series-detail', params: { id: episode.value.series_id } })
  } else {
    void router.push({ name: 'series-list' })
  }
}

function goWorkspace() {
  const projectId = episode.value?.project_id
  if (!projectId) {
    message.warning('该剧集尚未绑定生产项目，先完成续写生成剧本')
    return
  }
  void router.push({ name: 'script', params: { id: projectId } })
}

function goContinue() {
  if (!episode.value) return
  void router.push({
    name: 'series-continue',
    params: { id: episode.value.series_id },
    query: { episodeId: episode.value.id },
  })
}

onMounted(async () => {
  await loadAll()
})
</script>

<template>
  <div class="episode-review-page">
    <div class="page-head">
      <n-button quaternary size="small" @click="goBack">← 返回系列</n-button>
      <h1 class="page-title">剧集审核</h1>
      <p class="hint">只有点击确认的剧本才会写入正式剧情状态；警告可忽略，冲突需先处理。</p>
    </div>

    <div v-if="loading" class="state-box"><n-spin size="small" /><span>加载审核信息…</span></div>
    <div v-else-if="loadError" class="state-box error">
      <span>{{ loadError }}</span>
      <n-button size="small" @click="loadAll">重试</n-button>
    </div>

    <template v-else-if="episode">
      <n-card size="small" class="section-card">
        <n-space vertical size="small">
          <div class="title-row">
            <span class="episode-no">第 {{ episode.episode_number }} 集</span>
            <span class="episode-title">{{ episode.title || '未命名剧集' }}</span>
          </div>
          <n-space size="small" align="center">
            <n-tag size="small">剧本：{{ label(SCRIPT_STATUS_LABEL, episode.script_status) }}</n-tag>
            <n-tag size="small">生产：{{ label(PRODUCTION_STATUS_LABEL, episode.production_status) }}</n-tag>
            <n-tag size="small">连续性：{{ label(CONTINUITY_LABEL, episode.continuity_status) }}</n-tag>
            <n-tag size="small" type="info">剧情状态版本 v{{ seriesStateVersion ?? '-' }}</n-tag>
          </n-space>
          <div v-if="episode.synopsis" class="hint">{{ episode.synopsis }}</div>
          <n-space>
            <n-button size="small" @click="goWorkspace">进入单集生产</n-button>
            <n-button size="small" quaternary @click="goContinue">回到续写</n-button>
          </n-space>
        </n-space>
      </n-card>

      <n-card size="small" title="剧本版本" class="section-card">
        <div v-if="!scripts.length"><n-empty description="暂无剧本版本，请先完成续写生成" /></div>
        <n-space v-else vertical size="small">
          <div class="version-list">
            <div
              v-for="s in scripts"
              :key="s.id"
              class="version-item"
              :class="{ active: s.id === selectedScriptId }"
              @click="selectedScriptId = s.id"
            >
              <span class="version-badge">v{{ s.version }}</span>
              <n-tag size="tiny">{{ label(SCRIPT_STATUS_LABEL, s.status) }}</n-tag>
              <span class="hint">{{ s.created_at }}</span>
              <span v-if="s.model" class="hint">{{ s.model }}</span>
            </div>
          </div>

          <template v-if="selectedScript">
            <div class="block-title">剧本内容</div>
            <pre class="script-block">{{ selectedScript.content || '（该版本无正文内容）' }}</pre>
            <div v-if="selectedScript.synopsis" class="hint">梗概：{{ selectedScript.synopsis }}</div>
            <div class="hint" v-if="selectedScript.source_context_manifest_id">
              使用上下文清单：{{ selectedScript.source_context_manifest_id }}
            </div>
          </template>
        </n-space>
      </n-card>

      <n-card size="small" title="连续性检查" class="section-card">
        <n-space vertical size="small">
          <n-space align="center">
            <n-button size="small" :loading="checking" @click="runCheck">执行连续性检查</n-button>
            <n-tag size="small" :type="conflictCount ? 'error' : warningCount ? 'warning' : 'success'">
              冲突 {{ conflictCount }} / 警告 {{ warningCount }}
            </n-tag>
            <span v-if="issuesStatus" class="hint">检查状态：{{ label(CONTINUITY_LABEL, issuesStatus) }}</span>
          </n-space>
          <div v-if="checkError" class="state-box error"><span>{{ checkError }}</span></div>
          <div v-if="!issues.length" class="hint">暂无连续性问题记录</div>
          <div v-for="(issue, idx) in issues" :key="idx" class="issue-item">
            <n-space size="small" align="center">
              <n-tag size="tiny" :type="severityTagType(String(issue.severity ?? ''))">
                {{ label(SEVERITY_LABEL, String(issue.severity ?? '')) }}
              </n-tag>
              <span class="issue-type">{{ issue.type ?? '-' }}</span>
            </n-space>
            <div class="issue-msg">{{ issue.message ?? '' }}</div>
            <div v-if="formatEventIds(issue.source_event_ids)" class="hint">
              关联事件：{{ formatEventIds(issue.source_event_ids) }}
            </div>
            <div v-if="issue.suggestion" class="hint">建议：{{ issue.suggestion }}</div>
          </div>
        </n-space>
      </n-card>

      <n-card size="small" title="确认剧本" class="section-card">
        <n-space vertical size="small">
          <template v-if="confirmResult">
            <div class="result-block">
              <n-space size="small" align="center">
                <n-tag size="small" type="success">已确认</n-tag>
                <n-tag size="small">剧本版本 {{ confirmResult.script_version_id }}</n-tag>
                <n-tag size="small">状态 {{ label(SCRIPT_STATUS_LABEL, confirmResult.script_status) }}</n-tag>
                <n-tag size="small" type="info">剧情状态版本 v{{ confirmResult.series_state_version }}</n-tag>
                <n-tag size="small">快照 v{{ confirmResult.snapshot_version }}</n-tag>
                <n-tag size="small">提交事件 {{ confirmResult.committed_event_count }} 条</n-tag>
              </n-space>
              <div v-if="confirmResult.ignored_issue_ids?.length" class="hint">
                已忽略问题：{{ confirmResult.ignored_issue_ids.join('、') }}
              </div>
            </div>
          </template>

          <div v-if="confirmError" class="state-box error"><span>{{ confirmError }}</span></div>

          <span class="field-label">审核备注（可选）</span>
          <n-input v-model:value="reviewNote" type="textarea" :rows="2" placeholder="记录本次确认的判断依据" />
          <n-checkbox v-model:checked="ignoreWarnings">
            忽略非冲突类警告（冲突类问题不会被忽略）
          </n-checkbox>
          <n-space align="center">
            <n-button
              type="primary"
              :loading="confirming"
              :disabled="!canConfirm"
              @click="doConfirm"
            >
              确认当前剧本
            </n-button>
            <span v-if="!canConfirm" class="hint">仅草稿、分析中、待审核或已驳回版本可确认</span>
          </n-space>
        </n-space>
      </n-card>
    </template>

    <div v-else class="state-box"><span>未找到剧集</span></div>
  </div>
</template>

<style scoped>
.episode-review-page {
  padding: 24px;
  max-width: 1100px;
  margin: 0 auto;
}
.page-head {
  margin-bottom: 16px;
}
.page-title {
  font-size: 20px;
  margin: 8px 0 4px;
}
.hint {
  color: #888;
  font-size: 12px;
  margin: 0;
}
.section-card {
  margin-bottom: 16px;
}
.state-box {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  color: #666;
  font-size: 13px;
}
.state-box.error {
  color: #d03050;
}
.title-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.episode-no {
  font-size: 13px;
  color: #888;
}
.episode-title {
  font-size: 16px;
  font-weight: 600;
}
.version-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.version-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid #eee;
  border-radius: 6px;
  cursor: pointer;
}
.version-item.active {
  border-color: #2080f0;
  background: #f5faff;
}
.version-badge {
  font-size: 12px;
  font-weight: 600;
}
.block-title {
  font-size: 13px;
  font-weight: 600;
  margin: 8px 0 4px;
}
.script-block {
  background: #fafafa;
  border: 1px solid #eee;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
.issue-item {
  border: 1px solid #eee;
  border-radius: 6px;
  padding: 8px;
}
.issue-type {
  font-size: 12px;
  color: #666;
}
.issue-msg {
  font-size: 13px;
  margin: 4px 0;
}
.field-label {
  font-size: 12px;
  color: #666;
}
.result-block {
  border: 1px solid #d7f0d8;
  background: #f7fff7;
  border-radius: 6px;
  padding: 8px;
}
</style>
