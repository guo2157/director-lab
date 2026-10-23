<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  NButton,
  NCollapse,
  NCollapseItem,
  NEmpty,
  NSpin,
  NTag,
  useDialog,
  useMessage,
} from 'naive-ui'
import {
  confirmEstimate,
  confirmParseVersion,
  createScriptVersion,
  estimateScript,
  getProject,
  parseScript,
} from '@/api/projects'
import { listCharacters, listScenes, mergeCharacters } from '@/api/script'
import { getJob } from '@/api/jobs'
import type { Character, Scene, ScriptEstimate, ScriptVersion, Shot } from '@/types/script'

/* ---------- 路由与基础 ---------- */
const route = useRoute()
const dialog = useDialog()
const message = useMessage()
const queryClient = useQueryClient()
const projectId = computed(() => String(route.params.id ?? ''))

const MAX_CHARS = 50000

/* ---------- 本地扩展类型 ---------- */
interface SceneWithShots extends Scene {
  shots?: Shot[]
}
interface DiffItem {
  title?: string
  dialogue?: string
  seq?: number
  episode?: number
}
interface DiffSummary {
  added_shots?: DiffItem[] | number
  changed_shots?: DiffItem[] | number
  deleted_shots?: DiffItem[] | number
  added_episodes?: number[] | number
  changed_episodes?: number[] | number
  deleted_episodes?: number[] | number
}
interface ParseJobResult {
  project_analysis?: { summary?: string; theme?: string }
  characters?: Array<{ name: string; description?: string }>
  props?: Array<{ name: string; description?: string }>
  scenes?: Array<{ episode?: number; title: string; description?: string }>
  shots?: Array<Record<string, unknown>>
  episodes?: Array<{ episode: number; title?: string }>
  diff_summary?: DiffSummary
}

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) {
    const m = (e as { message?: string }).message
    if (m) return m
  }
  return e instanceof Error ? e.message : '请求失败'
}

/* ---------- 项目信息 ---------- */
const { data: projectData } = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
  enabled: computed(() => !!projectId.value),
})
const project = computed(() => projectData.value)

/* ---------- 左侧编辑器（含高亮镜像层） ---------- */
const text = ref('')
const editorRef = ref<HTMLTextAreaElement | null>(null)
const mirrorRef = ref<HTMLDivElement | null>(null)
const hlStart = ref<number | null>(null)
const hlEnd = ref<number | null>(null)
const textValid = computed(() => text.value.length >= 1 && text.value.length <= MAX_CHARS)

watch(text, () => {
  hlStart.value = null
  hlEnd.value = null
})

const segments = computed(() => {
  const s = hlStart.value
  const e = hlEnd.value
  const segs =
    s == null || e == null || s >= e
      ? [{ text: text.value, hl: false }]
      : [
          { text: text.value.slice(0, s), hl: false },
          { text: text.value.slice(s, e), hl: true },
          { text: text.value.slice(e), hl: false },
        ]
  if (text.value.endsWith('\n')) segs.push({ text: '\n', hl: false })
  return segs
})

function syncScroll(e: Event) {
  const ta = e.target as HTMLTextAreaElement
  if (mirrorRef.value) mirrorRef.value.scrollTop = ta.scrollTop
}

function locateScene(scene: SceneWithShots) {
  let idx = text.value.indexOf(scene.title)
  let len = scene.title.length
  if (idx < 0 && scene.summary) {
    const alt = scene.summary.slice(0, 12)
    idx = text.value.indexOf(alt)
    len = alt.length
  }
  if (idx < 0 || len <= 0) {
    message.warning('未在左侧原文中找到该场景对应段落')
    return
  }
  hlStart.value = idx
  hlEnd.value = idx + len
  void nextTick(() => {
    const mark = mirrorRef.value?.querySelector('.hl-mark') as HTMLElement | null
    if (mark && editorRef.value && mirrorRef.value) {
      const top = Math.max(0, mark.offsetTop - editorRef.value.clientHeight / 2)
      editorRef.value.scrollTop = top
      mirrorRef.value.scrollTop = top
    }
  })
}

/* ---------- 版本 / 评估 / 解析流程状态 ---------- */
const savedVersion = ref<ScriptVersion | null>(null)
const estimate = ref<ScriptEstimate | null>(null)
const estimateConfirmed = ref(false)
const parseJobId = ref<string | null>(null)
const candidateVersionId = ref<string | null>(null)

const { mutate: saveVersionMut, isPending: saving } = useMutation({
  mutationFn: () => createScriptVersion(projectId.value, { text: text.value }),
  onSuccess: (v) => {
    savedVersion.value = v
    estimate.value = null
    estimateConfirmed.value = false
    parseJobId.value = null
    candidateVersionId.value = null
    message.success(`剧本已保存为版本 v${v.version}`)
  },
  onError: (e) => message.error(errMsg(e)),
})

const { mutate: runEstimate, isPending: estimating } = useMutation({
  mutationFn: () => estimateScript(projectId.value, savedVersion.value!.script_version_id),
  onSuccess: (est) => {
    estimate.value = est
    message.success('评估完成，请确认后开始解析')
  },
  onError: (e) => message.error(errMsg(e)),
})

const { mutate: doConfirmEstimate, isPending: confirmingEstimate } = useMutation({
  mutationFn: () => confirmEstimate(projectId.value, estimate.value!.estimate_id),
  onSuccess: () => {
    estimateConfirmed.value = true
    message.success('评估已确认，可以开始解析')
  },
  onError: (e) => message.error(errMsg(e)),
})

const { mutate: runParse, isPending: parsing } = useMutation({
  mutationFn: (replace: boolean) =>
    parseScript(projectId.value, {
      script_version_id: savedVersion.value!.script_version_id,
      replace_existing: replace || undefined,
    }),
  onSuccess: (res) => {
    parseJobId.value = res.job_id
    candidateVersionId.value = res.parse_version.parse_version_id
    message.success('解析任务已提交')
  },
  onError: (e) => message.error(errMsg(e)),
})

function handleParse() {
  if (!savedVersion.value || !estimateConfirmed.value) return
  if (hasResults.value) {
    dialog.warning({
      title: '重新解析',
      content:
        '将基于当前剧本版本生成候选解析版本，确认切换前当前分镜与产物不受影响。是否继续？',
      positiveText: '重新解析',
      negativeText: '取消',
      onPositiveClick: () => runParse(true),
    })
  } else {
    runParse(false)
  }
}

/* ---------- 解析任务轮询 ---------- */
const { data: parseJobData } = useQuery({
  queryKey: computed(() => ['job', parseJobId.value]),
  queryFn: () => getJob(parseJobId.value!),
  enabled: computed(() => !!parseJobId.value),
  refetchInterval: (query) => {
    const data = query.state.data
    return data && (data.status === 'queued' || data.status === 'running') ? 2000 : false
  },
})
const parseJob = computed(() => parseJobData.value ?? null)
const candidateReady = computed(() => parseJob.value?.status === 'succeeded')
const candidateResult = computed<ParseJobResult | null>(
  () => (parseJob.value?.result as ParseJobResult | undefined) ?? null,
)
const diff = computed(() => candidateResult.value?.diff_summary ?? null)

function diffCount(v: DiffSummary['added_shots']): number {
  if (Array.isArray(v)) return v.length
  return typeof v === 'number' ? v : 0
}
function diffItems(v: DiffSummary['added_shots']): DiffItem[] {
  return Array.isArray(v) ? v : []
}
function diffItemLabel(it: DiffItem): string {
  return it.title || it.dialogue || `镜头 ${it.seq ?? ''}`
}
function diffEpisodes(v: DiffSummary['added_episodes']): string {
  if (Array.isArray(v)) return v.join('、')
  return typeof v === 'number' ? String(v) : ''
}

/* ---------- 确认 / 放弃候选版本 ---------- */
const { mutate: doConfirmParse, isPending: confirmingParse } = useMutation({
  mutationFn: () => confirmParseVersion(projectId.value, candidateVersionId.value!),
  onSuccess: () => {
    message.success('已切换到新解析版本，旧版本已标记为 superseded')
    parseJobId.value = null
    candidateVersionId.value = null
    void queryClient.invalidateQueries({ queryKey: ['scenes', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['characters', projectId.value] })
    void queryClient.invalidateQueries({ queryKey: ['project', projectId.value] })
  },
  onError: (e) => message.error(errMsg(e)),
})

function discardCandidate() {
  parseJobId.value = null
  candidateVersionId.value = null
  message.info('已放弃候选版本，当前分镜未受影响')
}

/* ---------- 当前解析结果（角色 / 场景） ---------- */
const { data: scenesData, isLoading: scenesLoading } = useQuery({
  queryKey: computed(() => ['scenes', projectId.value]),
  queryFn: () => listScenes(projectId.value, { page_size: 100 }),
  enabled: computed(() => !!projectId.value),
})
const scenes = computed<SceneWithShots[]>(
  () => (scenesData.value?.items as SceneWithShots[] | undefined) ?? [],
)
const hasResults = computed(() => scenes.value.length > 0)

const { data: charactersData } = useQuery({
  queryKey: computed(() => ['characters', projectId.value]),
  queryFn: () => listCharacters(projectId.value, { page_size: 100 }),
  enabled: computed(() => !!projectId.value),
})
const characters = computed(() => charactersData.value?.items ?? [])

const canParse = computed(
  () => !!savedVersion.value && estimateConfirmed.value && !candidateVersionId.value,
)

/* ---------- 角色合并 ---------- */
const mergeSourceId = ref<string | null>(null)
const mergeSource = computed(() => characters.value.find((c) => c.id === mergeSourceId.value) ?? null)
const { mutate: doMerge, isPending: merging } = useMutation({
  mutationFn: (p: { source: string; target: string }) => mergeCharacters(p.source, p.target),
  onSuccess: () => {
    message.success('角色已合并，相关镜头引用与提示词已同步改写')
    mergeSourceId.value = null
    void queryClient.invalidateQueries({ queryKey: ['characters', projectId.value] })
  },
  onError: (e) => message.error(errMsg(e)),
})

function handleMerge(target: Character) {
  const source = mergeSource.value
  if (!source) return
  dialog.warning({
    title: '合并角色',
    content: `将把『${source.name}』合并到『${target.name}』：镜头引用与提示词中的 @名称 将同步改写，源角色将被删除。是否继续？`,
    positiveText: '合并',
    negativeText: '取消',
    onPositiveClick: () => doMerge({ source: source.id, target: target.id }),
  })
}
</script>

<template>
  <section class="page script-page">
    <header class="script-header">
      <div class="header-left">
        <h1 class="page-title">剧本</h1>
        <span v-if="project" class="project-name" :title="project.name">{{ project.name }}</span>
        <NTag v-if="savedVersion" size="small" :bordered="false" type="info">
          v{{ savedVersion.version }}
        </NTag>
        <NTag v-if="estimateConfirmed" size="small" :bordered="false" type="success">评估已确认</NTag>
      </div>
      <div class="header-actions">
        <NButton
          size="small"
          :disabled="!textValid || saving"
          :loading="saving"
          @click="saveVersionMut()"
        >
          保存版本
        </NButton>
        <NButton
          size="small"
          :disabled="!savedVersion || estimating || estimateConfirmed"
          :loading="estimating"
          @click="runEstimate()"
        >
          评估
        </NButton>
        <NButton
          v-if="estimate && !estimateConfirmed"
          size="small"
          type="primary"
          secondary
          :loading="confirmingEstimate"
          @click="doConfirmEstimate()"
        >
          确认评估
        </NButton>
        <NButton size="small" type="primary" :disabled="!canParse" :loading="parsing" @click="handleParse()">
          {{ hasResults ? '重新解析' : '解析' }}
        </NButton>
      </div>
    </header>

    <div class="script-body">
      <!-- 左 40%：Markdown 编辑器 -->
      <div class="editor-pane">
        <div class="pane-toolbar">
          <span class="pane-label">Markdown 剧本</span>
          <span class="char-count" :class="{ over: !textValid }">{{ text.length }} / {{ MAX_CHARS }}</span>
        </div>
        <div class="editor-wrap">
          <div ref="mirrorRef" class="editor-mirror" aria-hidden="true">
            <template v-for="(seg, i) in segments" :key="i">
              <span v-if="seg.hl" class="hl-mark">{{ seg.text }}</span>
              <span v-else>{{ seg.text }}</span>
            </template>
          </div>
          <textarea
            ref="editorRef"
            v-model="text"
            class="editor-input"
            placeholder="粘贴或输入 Markdown 剧本原文…"
            spellcheck="false"
            @scroll="syncScroll"
          ></textarea>
        </div>
      </div>

      <!-- 右 60%：解析结果 -->
      <div class="result-pane">
        <!-- 候选解析版本 -->
        <div v-if="candidateVersionId" class="candidate-panel">
          <div class="candidate-head">
            <span class="candidate-badge">候选解析版本</span>
            <span class="candidate-note">确认切换前，当前分镜与产物不受影响</span>
          </div>
          <div v-if="parseJob && parseJob.status === 'failed'" class="candidate-failed">
            <span>解析失败：{{ parseJob.error?.message || '未知错误' }}</span>
            <NButton size="tiny" @click="handleParse()">重试</NButton>
          </div>
          <div
            v-else-if="parseJob && (parseJob.status === 'cancelled' || parseJob.status === 'lost')"
            class="candidate-failed"
          >
            <span>任务已{{ parseJob.status === 'cancelled' ? '取消' : '丢失' }}</span>
            <NButton size="tiny" @click="handleParse()">重新解析</NButton>
          </div>
          <template v-else-if="candidateReady">
            <div class="diff-tags">
              <NTag type="success" size="small" :bordered="false">新增 {{ diffCount(diff?.added_shots) }}</NTag>
              <NTag type="warning" size="small" :bordered="false">变化 {{ diffCount(diff?.changed_shots) }}</NTag>
              <NTag type="error" size="small" :bordered="false">删除 {{ diffCount(diff?.deleted_shots) }}</NTag>
            </div>
            <div
              v-if="diffEpisodes(diff?.added_episodes) || diffEpisodes(diff?.changed_episodes) || diffEpisodes(diff?.deleted_episodes)"
              class="diff-episodes"
            >
              <template v-if="diffEpisodes(diff?.added_episodes)">新增集：{{ diffEpisodes(diff?.added_episodes) }}　</template>
              <template v-if="diffEpisodes(diff?.changed_episodes)">变化集：{{ diffEpisodes(diff?.changed_episodes) }}　</template>
              <template v-if="diffEpisodes(diff?.deleted_episodes)">删除集：{{ diffEpisodes(diff?.deleted_episodes) }}</template>
            </div>
            <ul class="diff-list">
              <li v-for="(it, i) in diffItems(diff?.added_shots).slice(0, 8)" :key="'a' + i" class="diff-item add">
                ＋ {{ diffItemLabel(it) }}
              </li>
              <li v-for="(it, i) in diffItems(diff?.changed_shots).slice(0, 8)" :key="'c' + i" class="diff-item change">
                ± {{ diffItemLabel(it) }}
              </li>
              <li v-for="(it, i) in diffItems(diff?.deleted_shots).slice(0, 8)" :key="'d' + i" class="diff-item del">
                － {{ diffItemLabel(it) }}
              </li>
            </ul>
            <div class="candidate-preview">
              候选包含 {{ candidateResult?.scenes?.length ?? 0 }} 个场景 ·
              {{ candidateResult?.characters?.length ?? 0 }} 个角色 ·
              {{ candidateResult?.shots?.length ?? 0 }} 个镜头
            </div>
            <div class="rule-note">
              确认切换后：新增镜头→pending；变化镜头→redo、旧产物标 stale；删除镜头产物→archived；旧版本标记
              superseded。
            </div>
            <div class="candidate-actions">
              <NButton size="small" @click="discardCandidate">放弃候选</NButton>
              <NButton size="small" type="primary" :loading="confirmingParse" @click="doConfirmParse()">
                确认切换
              </NButton>
            </div>
          </template>
          <div v-else class="candidate-loading">
            <NSpin size="small" />
            <span>解析进行中…</span>
          </div>
        </div>

        <!-- 评估结果 -->
        <div v-if="estimate && !estimateConfirmed" class="estimate-panel">
          <div class="pane-label">评估结果</div>
          <div class="estimate-grid">
            <span>可制作集数 {{ estimate.episode_count }}</span>
            <span>场景 {{ estimate.scene_count }}</span>
            <span>镜头 {{ estimate.shot_count }}</span>
            <span>预计时长 {{ estimate.estimated_duration_seconds }}s</span>
          </div>
        </div>

        <!-- 角色区 -->
        <div class="section">
          <div class="section-head">
            <span class="pane-label">角色（{{ characters.length }}）</span>
            <NButton v-if="mergeSourceId" size="tiny" quaternary @click="mergeSourceId = null">
              取消合并
            </NButton>
          </div>
          <div v-if="mergeSource" class="merge-banner">
            已选择『{{ mergeSource.name }}』为源角色，点击目标角色的『合并到此』完成合并（引用与提示词将同步改写）。
          </div>
          <div v-if="characters.length" class="char-list">
            <div v-for="ch in characters" :key="ch.id" class="char-card">
              <div class="char-avatar">{{ ch.name.slice(0, 1) }}</div>
              <div class="char-info">
                <div class="char-name">{{ ch.name }}</div>
                <div class="char-desc">{{ ch.description || '暂无描述' }}</div>
              </div>
              <NButton
                v-if="!mergeSourceId"
                size="tiny"
                quaternary
                :disabled="characters.length < 2"
                @click="mergeSourceId = ch.id"
              >
                合并
              </NButton>
              <NButton
                v-else-if="mergeSourceId !== ch.id"
                size="tiny"
                type="primary"
                secondary
                :loading="merging"
                @click="handleMerge(ch)"
              >
                合并到此
              </NButton>
            </div>
          </div>
          <NEmpty v-else size="small" description="解析后生成角色" />
        </div>

        <!-- 场景区 -->
        <div class="section">
          <div class="section-head">
            <span class="pane-label">场景（{{ scenes.length }}）</span>
          </div>
          <NSpin v-if="scenesLoading" size="small" class="section-spin" />
          <NCollapse
            v-else-if="scenes.length"
            class="scene-collapse"
            :default-expanded-names="[scenes[0].id]"
          >
            <NCollapseItem v-for="sc in scenes" :key="sc.id" :name="sc.id">
              <template #header>
                <span class="scene-title" @click.stop="locateScene(sc)">
                  <span class="scene-seq">#{{ sc.seq }}</span>
                  <span class="scene-name">{{ sc.title }}</span>
                </span>
              </template>
              <template #header-extra>
                <NTag size="tiny" :bordered="false">{{ sc.shot_count ?? sc.shots?.length ?? 0 }} 镜头</NTag>
              </template>
              <div class="scene-body">
                <p v-if="sc.summary" class="scene-summary">{{ sc.summary }}</p>
                <div v-if="sc.episodes?.length" class="scene-episodes">
                  <NTag v-for="ep in sc.episodes" :key="ep" size="tiny" :bordered="false">
                    第 {{ ep }} 集
                  </NTag>
                </div>
                <ul v-if="sc.shots?.length" class="shot-list">
                  <li v-for="sh in sc.shots" :key="sh.id" class="shot-item">
                    <span class="shot-seq">S{{ sh.episode }}-{{ sh.seq }}</span>
                    <span class="shot-kind">{{ sh.shot_kind }}</span>
                    <span class="shot-text">{{ sh.title || sh.dialogue || sh.description || '—' }}</span>
                    <span class="shot-duration">{{ sh.duration }}s</span>
                  </li>
                </ul>
              </div>
            </NCollapseItem>
          </NCollapse>
          <NEmpty v-else size="small" description="解析后生成场景" />
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.script-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.script-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 12px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.page-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
  margin: 0;
}
.project-name {
  font-size: 13px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.script-body {
  flex: 1;
  display: flex;
  gap: 16px;
  min-height: 0;
}

/* 左侧编辑器 */
.editor-pane {
  width: 40%;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  overflow: hidden;
  min-height: 0;
}
.pane-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.pane-label {
  font-size: 13px;
  color: var(--text-secondary);
}
.char-count {
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.char-count.over {
  color: var(--danger);
}
.editor-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
}
.editor-mirror,
.editor-input {
  position: absolute;
  inset: 0;
  margin: 0;
  padding: 12px;
  box-sizing: border-box;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  tab-size: 4;
  border: none;
}
.editor-mirror {
  color: transparent;
  overflow: hidden;
  pointer-events: none;
}
.hl-mark {
  background: rgba(99, 102, 241, 0.4);
  border-radius: 2px;
}
.editor-input {
  width: 100%;
  height: 100%;
  background: transparent;
  color: var(--text-primary);
  caret-color: var(--brand-primary);
  outline: none;
  resize: none;
  overflow-y: auto;
}
.editor-input::placeholder {
  color: var(--text-disabled, #4b5563);
}

/* 右侧解析结果 */
.result-pane {
  width: 60%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
  min-height: 0;
}
.candidate-panel {
  background: var(--bg-panel);
  border: 1px solid var(--brand-primary);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.candidate-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.candidate-badge {
  font-size: 12px;
  color: var(--brand-primary);
  background: rgba(99, 102, 241, 0.15);
  border-radius: 999px;
  padding: 2px 10px;
}
.candidate-note {
  font-size: 12px;
  color: var(--text-secondary);
}
.candidate-failed {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
  color: var(--danger);
}
.candidate-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-secondary);
  padding: 4px 0;
}
.diff-tags {
  display: flex;
  gap: 8px;
}
.diff-episodes {
  font-size: 12px;
  color: var(--text-secondary);
}
.diff-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.diff-item {
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--bg-elevated);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.diff-item.add {
  color: var(--state-success, #22c55e);
}
.diff-item.change {
  color: var(--state-warning, #f59e0b);
}
.diff-item.del {
  color: var(--danger);
}
.candidate-preview {
  font-size: 12px;
  color: var(--text-secondary);
}
.rule-note {
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-elevated);
  border-radius: 8px;
  padding: 8px;
  line-height: 1.6;
}
.candidate-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.estimate-panel {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.estimate-grid {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--text-primary);
}
.section {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.section-spin {
  align-self: center;
  padding: 8px 0;
}
.merge-banner {
  font-size: 12px;
  color: var(--state-warning, #f59e0b);
  background: rgba(245, 158, 11, 0.1);
  border-radius: 8px;
  padding: 8px;
  line-height: 1.6;
}
.char-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.char-card {
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--bg-elevated);
  border-radius: 8px;
  padding: 10px;
}
.char-avatar {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(99, 102, 241, 0.18);
  color: var(--brand-primary);
  font-size: 16px;
  font-weight: 600;
}
.char-info {
  flex: 1;
  min-width: 0;
}
.char-name {
  font-size: 14px;
  color: var(--text-primary);
}
.char-desc {
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 2px;
}
.scene-collapse {
  background: transparent;
}
.scene-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  cursor: pointer;
}
.scene-seq {
  font-size: 12px;
  color: var(--brand-primary);
  font-variant-numeric: tabular-nums;
}
.scene-name {
  font-size: 14px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.scene-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.scene-summary {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin: 0;
}
.scene-episodes {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.shot-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.shot-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 4px 8px;
  background: var(--bg-elevated);
  border-radius: 6px;
}
.shot-seq {
  color: var(--brand-primary);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.shot-kind {
  color: var(--text-secondary);
  flex-shrink: 0;
}
.shot-text {
  flex: 1;
  min-width: 0;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.shot-duration {
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
</style>
