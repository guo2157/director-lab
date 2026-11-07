<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NModal, NTag, useMessage } from 'naive-ui'
import {
  createScriptVersion,
  getCurrentScript,
  getProject,
  normalizeScript,
  runScriptAgent,
  type ScriptNormalizeResult,
} from '@/api/projects'
import type { ScriptVersion } from '@/types/script'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import StyleConfigPanel from '@/components/StyleConfigPanel.vue'
import FlowNav from '@/components/FlowNav.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const flow = useScriptFlowStore()
const projectId = computed(() => String(route.params.id ?? ''))
const MAX_CHARS = 50000

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) {
    const m = (e as { message?: string }).message
    if (m) return m
  }
  return e instanceof Error ? e.message : '请求失败'
}

/* ---------- 四步流程：初始剧本 → 校验补充 → 保存 → 分析 ---------- */
type Stage = 'draft' | 'checked' | 'saved' | 'analyzing'
const stage = ref<Stage>('draft')
const steps = [
  { key: 'draft', label: '初始剧本' },
  { key: 'checked', label: '校验补充' },
  { key: 'saved', label: '保存版本' },
  { key: 'analyzing', label: '开始分析' },
] as const
const stageIndex = computed(() => {
  const i = steps.findIndex((s) => s.key === stage.value)
  return i < 0 ? 0 : i
})
const stageHint = computed(() => {
  if (normalizing.value) return 'AI 正在分析剧本并整理为标准格式，请稍候…'
  if (stage.value === 'draft') return '编辑剧本后建议先校验补充，再保存与分析'
  if (stage.value === 'checked') return '校验已完成，下一步保存版本'
  if (stage.value === 'saved') return '版本已保存，可以开始分析'
  return '分析已提交，正在跳转解析页'
})

/* 校验结果与版本状态 */
const normalizeResult = ref<ScriptNormalizeResult | null>(null)
const savedVersion = ref<ScriptVersion | null>(null)
const compareVisible = ref(false)

/* 项目切换时重置流程状态（防止其他项目残留状态误显示） */
watch(
  projectId,
  (id) => {
    if (id) flow.ensureProject(id)
    stage.value = 'draft'
    normalizeResult.value = null
    savedVersion.value = null
    compareVisible.value = false
  },
  { immediate: true },
)

/* ---------- 项目信息 ---------- */
const { data: projectData } = useQuery({
  queryKey: computed(() => ['project', projectId.value]),
  queryFn: () => getProject(projectId.value),
  enabled: computed(() => !!projectId.value),
})
const project = computed(() => projectData.value)

/* ---------- 创作风格配置（项目级唯一基线） ---------- */
const showStylePanel = ref(false)

/* ---------- 编辑器 ---------- */
const text = ref('')
const editorRef = ref<HTMLTextAreaElement | null>(null)
const mirrorRef = ref<HTMLDivElement | null>(null)
const hlStart = ref<number | null>(null)
const hlEnd = ref<number | null>(null)
const textValid = computed(() => text.value.length >= 1 && text.value.length <= MAX_CHARS)

/* 正文变更：除非是「采用优化稿」引起的变化，否则回退到初始剧本阶段并作废校验与保存结果 */
watch(text, (val) => {
  hlStart.value = null
  hlEnd.value = null
  const res = normalizeResult.value
  if (res && val === res.normalized_text) return
  if (stage.value !== 'draft') {
    stage.value = 'draft'
    normalizeResult.value = null
    savedVersion.value = null
  }
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

/* ---------- 第二步：校验补充（不落库，仅预览） ---------- */
const { mutate: runNormalizeMut, isPending: normalizing } = useMutation({
  mutationFn: () => normalizeScript(projectId.value, { source_text: text.value }),
  onMutate: () => {
    message.info('AI 正在分析剧本格式，请稍候…（通常需 10–30 秒）')
  },
  onSuccess: (res) => {
    normalizeResult.value = res
    compareVisible.value = true
    if (res.checks.blocked) {
      message.warning('优化稿未通过守恒校验，只能保留原文')
    } else if (!res.changes.length) {
      message.success('剧本格式已符合规范，无需调整')
    } else {
      message.success(`校验完成，共 ${res.changes.length} 处调整，请确认`)
    }
  },
  onError: (e) => {
    message.error(`校验失败: ${errMsg(e)}`)
  },
})

function handleNormalize() {
  if (normalizeResult.value) {
    compareVisible.value = true
    return
  }
  runNormalizeMut()
}

function adoptNormalized() {
  const res = normalizeResult.value
  if (!res) return
  if (res.checks.blocked) {
    message.error('优化稿缺失原文人物或台词，不能采用')
    return
  }
  applyNormalized(res.normalized_text)
}
/**
 * 强行采用：守恒校验未通过时仍允许替换正文。
 * 校验基准已改为正则从原文抽取 + 标点归一，仍报阻断说明确实有内容缺失，
 * 因此保留二次确认语义，但不再把按钮彻底禁用——用户可能就是要删掉某句台词。
 */
function forceAdoptNormalized() {
  const res = normalizeResult.value
  if (!res) return
  applyNormalized(res.normalized_text)
  const n =
    (res.checks.missing_characters?.length ?? 0) + (res.checks.missing_dialogues?.length ?? 0)
  message.warning(`已强行采用，忽略 ${n} 处差异，请仔细核对后再保存`)
}
function applyNormalized(next: string) {
  text.value = next
  stage.value = 'checked'
  compareVisible.value = false
  message.success('已采用优化稿，请确认后保存版本')
}

function keepOriginal() {
  stage.value = 'checked'
  compareVisible.value = false
  message.info('已保留原文，继续保存版本')
}

function changeLabel(cat: string): string {
  if (cat === 'format') return '格式'
  if (cat === 'clarity') return '明确化'
  if (cat === 'completion') return '补全'
  return cat
}
function changeTagType(cat: string): 'default' | 'info' | 'warning' {
  if (cat === 'completion') return 'warning'
  if (cat === 'clarity') return 'info'
  return 'default'
}

/* ---------- 第三步：保存版本 ---------- */
const { mutate: saveVersionMut, isPending: saving } = useMutation({
  mutationFn: () => createScriptVersion(projectId.value, { text: text.value }),
  onSuccess: (v) => {
    flow.scriptVersionId = v.script_version_id
    flow.scriptText = text.value
    flow.resetAfterSave()
    savedVersion.value = v
    stage.value = 'saved'
    message.success(`已保存为第 ${v.version} 版`)
  },
  onError: (e) => message.error(errMsg(e)),
})

/* ---------- 第四步：开始分析（必须使用已保存版本，保证版本与正文一致） ---------- */
const { mutate: runAgentMut, isPending: analyzing } = useMutation({
  mutationFn: () => {
    const v = savedVersion.value
    if (!v) throw new Error('请先保存剧本版本')
    return runScriptAgent(projectId.value, v.script_version_id, text.value)
  },
  onSuccess: (res) => {
    flow.parseJobId = res.job_id
    stage.value = 'analyzing'
    message.success('已开始分析，Agent 将自动完成评估与解析')
    setTimeout(() => router.push(`/project/${projectId.value}/parse`), 600)
  },
  onError: (e) => {
    message.error(errMsg(e))
  },
})

/* ---------- 加载当前剧本 ---------- */
const { data: scriptData } = useQuery({
  queryKey: computed(() => ['currentScript', projectId.value]),
  queryFn: () => getCurrentScript(projectId.value),
  enabled: computed(() => !!projectId.value),
})
watch(
  () => scriptData.value,
  (sv) => {
    if (sv?.exists && sv.script_version_id) {
      text.value = sv.text ?? ''
      flow.scriptVersionId = sv.script_version_id
      flow.scriptText = sv.text ?? ''
    }
  },
  { immediate: true },
)
</script>

<template>
  <section class="script-page">
    <header class="script-header">
      <div class="header-left">
        <h1 class="page-title">剧本</h1>
        <FlowNav :project-id="projectId" />
        <span class="project-name">{{ project?.name ?? '' }}</span>
      </div>
      <div class="header-actions">
        <NButton size="small" quaternary @click="showStylePanel = !showStylePanel">
          {{ showStylePanel ? '收起风格配置' : '创作风格配置' }}
        </NButton>
        <NButton
          size="small"
          :loading="normalizing"
          :disabled="!textValid || normalizing"
          @click="handleNormalize"
        >
          {{ normalizing ? '校验中…' : normalizeResult ? '查看校验结果' : '校验补充' }}
        </NButton>
        <NButton
          size="small"
          :loading="saving"
          :disabled="!textValid || saving"
          @click="saveVersionMut()"
        >
          {{ saving ? '保存中…' : savedVersion ? '重新保存' : '保存版本' }}
        </NButton>
        <NButton
          size="small"
          type="primary"
          :loading="analyzing"
          :disabled="!savedVersion || analyzing"
          @click="runAgentMut()"
        >
          开始分析
        </NButton>
      </div>
    </header>

    <!-- 四步流程指示：初始剧本 → 校验补充 → 保存 → 分析 -->
    <div class="stage-bar">
      <div
        v-for="(s, i) in steps"
        :key="s.key"
        class="stage-item"
        :class="{ active: stageIndex === i, done: stageIndex > i }"
      >
        <span class="stage-index">{{ stageIndex > i ? '✓' : i + 1 }}</span>
        <span class="stage-label">{{ s.label }}</span>
        <span v-if="s.key === 'saved' && savedVersion" class="stage-extra">v{{ savedVersion.version }}</span>
      </div>
      <span class="stage-hint">{{ stageHint }}</span>
    </div>

    <div v-if="showStylePanel" class="style-drawer">
      <StyleConfigPanel :project-id="projectId" />
    </div>
    <div class="script-body">
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
            placeholder="粘贴或输入 Markdown 剧本原文，也可以只写一个标题或一句话，点『校验补充』由 AI 整理为标准剧本格式…"
            spellcheck="false"
            @scroll="syncScroll"
          ></textarea>
        </div>
      </div>
    </div>

    <!-- 校验补充：原文与优化稿对比 + 变更清单 + 守恒校验结果 -->
    <NModal
      v-model:show="compareVisible"
      preset="card"
      title="剧本校验补充"
      :bordered="false"
      style="width: min(1100px, 96vw)"
    >
      <div v-if="normalizeResult" class="compare">
        <div class="compare-summary">
          <NTag size="small" :type="normalizeResult.checks.blocked ? 'error' : 'success'">
            {{ normalizeResult.checks.blocked ? '守恒校验未通过' : '守恒校验通过' }}
          </NTag>
          <span>处理层级：第 {{ normalizeResult.level }} 层</span>
          <span>{{ normalizeResult.original_chars }} → {{ normalizeResult.normalized_chars }} 字</span>
        </div>

        <div v-if="normalizeResult.checks.blocked" class="blocked-tip">
          <div v-if="normalizeResult.checks.missing_characters?.length">
            丢失人物：{{ normalizeResult.checks.missing_characters.join('、') }}
          </div>
          <div v-if="normalizeResult.checks.missing_dialogues?.length">
            丢失台词：{{ normalizeResult.checks.missing_dialogues.join(' / ') }}
          </div>
          <div>
            优化稿缺失了上述人物或台词。若这是你想要的删改，可点右下角「仍然采用」强制替换。
          </div>
        </div>
        <div v-else-if="normalizeResult.checks.punct_warnings?.length" class="punct-tip">
          <div>以下台词仅有标点或空白差异，不影响采用：</div>
          <div class="punct-list">{{ normalizeResult.checks.punct_warnings.join(' / ') }}</div>
        </div>

        <div class="compare-body">
          <div class="compare-pane">
            <div class="pane-title">原文</div>
            <pre class="pane-text">{{ text }}</pre>
          </div>
          <div class="compare-pane">
            <div class="pane-title">优化稿</div>
            <pre class="pane-text">{{ normalizeResult.normalized_text }}</pre>
          </div>
        </div>

        <div v-if="normalizeResult.changes.length" class="changes">
          <div class="changes-title">变更清单（{{ normalizeResult.changes.length }}）</div>
          <div v-for="(c, i) in normalizeResult.changes" :key="i" class="change-item">
            <NTag size="tiny" :type="changeTagType(c.category)">{{ changeLabel(c.category) }}</NTag>
            <span class="change-summary">{{ c.summary }}</span>
            <NTag v-if="c.ai_generated" size="tiny" type="warning">AI 补全</NTag>
            <span v-if="c.evidence" class="change-evidence">依据：{{ c.evidence }}</span>
          </div>
        </div>
        <div v-else class="changes-empty">未发现需要调整的内容，格式已符合规范</div>

        <div v-if="normalizeResult.questions.length" class="questions">
          <div class="changes-title">需要你补充的关键要素</div>
          <ul>
            <li v-for="(q, i) in normalizeResult.questions" :key="i">{{ q }}</li>
          </ul>
          <div class="questions-tip">补充后可修改原文并再次点『校验补充』，获得更贴合原意的优化稿</div>
        </div>
      </div>
      <template #footer>
        <div class="modal-footer">
          <NButton @click="compareVisible = false">稍后处理</NButton>
          <NButton @click="keepOriginal">保留原文</NButton>
          <NButton
            v-if="!normalizeResult?.checks.blocked"
            type="primary"
            :disabled="!normalizeResult"
            @click="adoptNormalized"
          >
            采用优化稿
          </NButton>
          <NButton v-else type="warning" secondary @click="forceAdoptNormalized">
            仍然采用
          </NButton>
        </div>
      </template>
    </NModal>
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
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle, #e5e7eb);
  flex-shrink: 0;
}
.header-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.page-title { font-size: 16px; font-weight: 600; margin: 0; }
.project-name { font-size: 12px; color: var(--text-secondary, #64748b); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

/* 四步流程条 */
.stage-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle, #e5e7eb);
  background: var(--bg-elevated, #f8fafc);
  flex-shrink: 0;
  flex-wrap: wrap;
}
.stage-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-tertiary, #999);
}
.stage-item.active { color: var(--brand-primary, #6366f1); font-weight: 600; }
.stage-item.done { color: #22C55E; }
.stage-index {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  border: 1px solid currentColor;
  flex-shrink: 0;
}
.stage-extra { font-size: 11px; color: var(--text-secondary, #64748b); }
.stage-hint { margin-left: auto; font-size: 12px; color: var(--text-tertiary, #999); }

.style-drawer {
  flex-shrink: 0;
  max-height: 52vh;
  overflow-y: auto;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle, #e5e7eb);
  background: var(--bg-elevated, #f8fafc);
}
.script-body { display: flex; flex: 1; min-height: 0; }
.editor-pane {
  flex: 1;
  width: 100%;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.pane-toolbar { display: flex; align-items: center; justify-content: space-between; padding: 8px 12px; border-bottom: 1px solid var(--border-subtle, #e5e7eb); flex-shrink: 0; }
.pane-label { font-size: 13px; font-weight: 600; color: var(--text-primary, #333); }
.char-count { font-size: 12px; color: var(--text-secondary, #999); font-variant-numeric: tabular-nums; }
.char-count.over { color: var(--brand-error, #d03050); }
.editor-wrap { position: relative; flex: 1; overflow: hidden; }
.editor-mirror { position: absolute; inset: 0; padding: 12px 16px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 14px; line-height: 1.8; white-space: pre-wrap; word-break: break-word; overflow-y: auto; color: transparent; pointer-events: none; z-index: 0; }
.editor-mirror .hl-mark { background: rgba(99, 102, 241, 0.18); border-radius: 3px; }
.editor-input { position: absolute; inset: 0; padding: 12px 16px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 14px; line-height: 1.8; border: none; outline: none; resize: none; background: transparent; color: var(--text-primary, #333); caret-color: var(--brand-primary, #6366f1); z-index: 1; }
.editor-input::placeholder { color: var(--text-secondary, #999); }

/* 校验对比弹窗 */
.compare { display: flex; flex-direction: column; gap: 12px; max-height: 64vh; overflow-y: auto; }
.compare-summary { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--text-secondary, #64748b); flex-wrap: wrap; }
.blocked-tip { padding: 8px 10px; border-radius: 8px; background: rgba(208, 48, 80, 0.08); color: #d03050; font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
/* 标点/空白差异提示：不阻断采用，用琥珀色与红色阻断区分 */
.punct-tip { padding: 8px 10px; border-radius: 8px; background: rgba(240, 160, 32, 0.1); color: #b06a00; font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
.punct-list { color: var(--text-secondary, #64748b); word-break: break-all; }
.compare-body { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.compare-pane { display: flex; flex-direction: column; min-width: 0; border: 1px solid var(--border-subtle, #e5e7eb); border-radius: 8px; overflow: hidden; }
.pane-title { padding: 6px 10px; font-size: 12px; font-weight: 600; background: var(--bg-elevated, #f8fafc); border-bottom: 1px solid var(--border-subtle, #e5e7eb); }
.pane-text { margin: 0; padding: 10px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12.5px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow-y: auto; }
.changes { display: flex; flex-direction: column; gap: 6px; }
.changes-title { font-size: 12px; font-weight: 600; color: var(--text-primary, #333); }
.change-item { display: flex; align-items: baseline; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.change-summary { color: var(--text-primary, #333); }
.change-evidence { color: var(--text-tertiary, #999); font-size: 11px; }
.changes-empty { font-size: 12px; color: var(--text-tertiary, #999); padding: 6px 0; }
.questions { display: flex; flex-direction: column; gap: 4px; padding: 10px; border-radius: 8px; background: rgba(245, 158, 11, 0.08); }
.questions ul { margin: 0; padding-left: 18px; font-size: 12px; color: #b45309; }
.questions-tip { font-size: 11px; color: var(--text-tertiary, #999); }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; }

@media (max-width: 768px) {
  .script-page { height: auto; min-height: calc(100vh - var(--app-header-height, 56px)); }
  .script-header { flex-wrap: wrap; gap: 8px; }
  .header-actions { flex-wrap: wrap; }
  .stage-hint { display: none; }
  .compare-body { grid-template-columns: 1fr; }
  .pane-text { max-height: 28vh; }
}
</style>
