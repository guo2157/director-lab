<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, useMessage } from 'naive-ui'
import { createScriptVersion, getCurrentScript, getProject, runScriptAgent } from '@/api/projects'
import { useScriptFlowStore } from '@/stores/scriptFlow'
import FlowNav from '@/components/FlowNav.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const flow = useScriptFlowStore()
const projectId = computed(() => String(route.params.id ?? ''))
const MAX_CHARS = 50000

/* 项目切换时重置流程状态（防止其他项目残留的剧本/评估状态误显示对号） */
watch(
  projectId,
  (id) => {
    if (id) flow.ensureProject(id)
  },
  { immediate: true },
)

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

/* ---------- 编辑器 ---------- */
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

/* ---------- 保存并开始分析（Agent 一体化：评估+解析合并，集数由 Agent 决定） ---------- */
const { mutate: saveVersionMut, isPending: saving } = useMutation({
  mutationFn: () => createScriptVersion(projectId.value, { text: text.value }),
  onSuccess: (v) => {
    flow.scriptVersionId = v.script_version_id
    flow.scriptText = text.value
    flow.resetAfterSave()
    // 保存成功后直接触发 Agent 一体化分析（无需评估/确认集数）
    runAgentMut({ scriptVersionId: v.script_version_id, text: text.value })
  },
  onError: (e) => message.error(errMsg(e)),
})
const { mutate: runAgentMut, isPending: analyzing } = useMutation({
  mutationFn: (p: { scriptVersionId: string; text: string }) =>
    runScriptAgent(projectId.value, p.scriptVersionId, p.text),
  onSuccess: (res) => {
    flow.parseJobId = res.job_id
    message.success('已开始分析，Agent 将自动完成评估与解析')
    setTimeout(() => router.push(`/project/${projectId.value}/parse`), 600)
  },
  onError: (e) => {
    message.error(errMsg(e))
    // 分析提交失败时仍跳转解析页，可从解析页重试
    setTimeout(() => router.push(`/project/${projectId.value}/parse`), 800)
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
        <NButton
          size="small"
          type="primary"
          :disabled="!textValid || saving || analyzing"
          :loading="saving || analyzing"
          @click="saveVersionMut()"
        >
          {{ saving ? '保存中…' : analyzing ? '开始分析…' : '开始分析' }}
        </NButton>
      </div>
    </header>
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
            placeholder="粘贴或输入 Markdown 剧本原文…"
            spellcheck="false"
            @scroll="syncScroll"
          ></textarea>
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
  overflow: hidden;
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
@media (max-width: 768px) {
  .script-page { height: auto; min-height: calc(100vh - var(--app-header-height, 56px)); }
  .script-header { flex-wrap: wrap; gap: 8px; }
  .header-actions { flex-wrap: wrap; }
}
</style>
