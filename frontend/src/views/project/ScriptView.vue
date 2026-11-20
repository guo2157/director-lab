<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NButton, useMessage } from 'naive-ui'
import {
  createScriptVersion,
  getCurrentScript,
  normalizeScript,
  type ScriptNormalizeResult,
} from '@/api/projects'
import type { ScriptVersion } from '@/types/script'
import StyleConfigPanel from '@/components/StyleConfigPanel.vue'
import ScriptEditor from '@/components/script/ScriptEditor.vue'
import ScriptToolbar from '@/components/script/ScriptToolbar.vue'
import ScriptStageBar from '@/components/script/ScriptStageBar.vue'
import ScriptReader from '@/components/script/ScriptReader.vue'
import ScriptCompareModal from '@/components/script/ScriptCompareModal.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const queryClient = useQueryClient()
const projectId = computed(() => String(route.params.id ?? ''))
const MAX_CHARS = 50000

function errMsg(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e) {
    const m = (e as { message?: string }).message
    if (m) return m
  }
  return e instanceof Error ? e.message : '请求失败'
}

/* ---------- 三步流程：初始剧本 → 校验补充 → 保存版本 ---------- */
/*
 * 解析已独立成制作节点（2026-11-20），本页不再有「开始分析」，
 * 因此阶段条从四步收回三步：本页只负责产出一个已保存的剧本版本。
 */
type Stage = 'draft' | 'checked' | 'saved'
const stage = ref<Stage>('draft')
/*
 * 阶段条描述「现在处于哪个阶段」，按钮描述「可以做什么」。
 * 两者语义不同，因此标签不与按钮同名——否则同一屏出现两遍"校验补充/保存版本"。
 */
const steps = [
  { key: 'draft', label: '剧本编辑' },
  { key: 'checked', label: '内容校验' },
  { key: 'saved', label: '版本已存' },
] as const
const stageIndex = computed(() => {
  const i = steps.findIndex((s) => s.key === stage.value)
  return i < 0 ? 0 : i
})

/* 校验结果与版本状态 */
const normalizeResult = ref<ScriptNormalizeResult | null>(null)
const savedVersion = ref<ScriptVersion | null>(null)
const compareVisible = ref(false)

/* 项目切换时重置本页状态（防止其他项目残留状态误显示） */
watch(
  projectId,
  () => {
    stage.value = 'draft'
    normalizeResult.value = null
    savedVersion.value = null
    compareVisible.value = false
  },
  { immediate: true },
)

/* ---------- 项目信息 ----------
   项目名由 AppLayout 面包屑统一持有（同一个 ['project', id] 缓存），此页不再重复取用 */
/* ---------- 创作风格配置（项目级唯一基线） ---------- */
const showStylePanel = ref(false)

/* ---------- 编辑器 ---------- */
const text = ref('')
const textValid = computed(() => text.value.length >= 1 && text.value.length <= MAX_CHARS)

/**
 * 从服务端回填正文时置 true，阻止下方的 text watch 把刚恢复的「已保存版本」状态清掉。
 * 为什么必须有：scriptData watch 会先写 text 再写 savedVersion/stage，
 * 而 text 的 watch 在下一轮 flush 才执行，此时 stage 已是 'saved'，
 * 于是被判定为「用户改了正文」，把 savedVersion 置空 —— 「开始分析」在刷新后永远禁用。
 */
let hydrating = false

/* 正文变更：除非是「采用优化稿」引起的变化，否则回退到初始剧本阶段并作废校验与保存结果 */
watch(text, (val) => {
  if (hydrating) return
  const res = normalizeResult.value
  if (res && val === res.normalized_text) return
  if (stage.value !== 'draft') {
    stage.value = 'draft'
    normalizeResult.value = null
    savedVersion.value = null
  }
})

/* ---------- 视图模式：编辑 / 阅读 / 分栏 ---------- */
type ViewMode = 'edit' | 'read' | 'split'
const modes: Array<{ value: ViewMode; label: string }> = [
  { value: 'edit', label: '编辑' },
  { value: 'read', label: '阅读' },
  { value: 'split', label: '分栏' },
]
const VIEW_KEY = 'dl.script.view'
const viewMode = ref<ViewMode>('edit')
/** 用户手动选过视图就不再自动切换 */
const userPicked = ref(false)
/** 分栏需要 ≥1080px，窄屏只给编辑/阅读 */
const wide = ref(true)
/** 因窗口变窄被自动降级为阅读：宽度恢复时若用户没手动选过，回到分栏 */
const forcedRead = ref(false)

function setMode(m: ViewMode) {
  if (m === 'split' && !wide.value) return
  viewMode.value = m
  userPicked.value = true
  try {
    localStorage.setItem(VIEW_KEY, m)
  } catch {
    /* 隐私模式写入失败不影响使用 */
  }
}

onMounted(() => {
  const mq = window.matchMedia('(min-width: 1101px)')
  wide.value = mq.matches
  mq.addEventListener('change', (e) => {
    wide.value = e.matches
    if (!e.matches && viewMode.value === 'split') {
      viewMode.value = 'read'
      forcedRead.value = true
    } else if (e.matches && forcedRead.value && !userPicked.value) {
      viewMode.value = 'split'
      forcedRead.value = false
    }
  })
  try {
    const saved = localStorage.getItem(VIEW_KEY) as ViewMode | null
    if (saved === 'edit' || saved === 'read' || saved === 'split') {
      userPicked.value = true
      viewMode.value = saved === 'split' && !wide.value ? 'read' : saved
    }
  } catch {
    /* 读不到就用默认值 */
  }
})

/** 正文就绪且用户没选过：宽屏默认分栏（边改边看），窄屏默认阅读 */
watch(
  text,
  (val) => {
    if (userPicked.value || !val.trim()) return
    viewMode.value = wide.value ? 'split' : 'read'
  },
  { immediate: true },
)

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

/* ---------- 第三步：保存版本（本页唯一出口动作） ---------- */
const { mutate: saveVersionMut, isPending: saving } = useMutation({
  mutationFn: () => createScriptVersion(projectId.value, { text: text.value }),
  onSuccess: (v) => {
    savedVersion.value = v
    stage.value = 'saved'
    // 左栏「剧本」完成态与解析页的可用性都读这份缓存，保存后必须立刻失效，
    // 否则左栏要等下一次 staleTime 到期才会变成「已完成」。
    void queryClient.invalidateQueries({ queryKey: ['currentScript', projectId.value] })
    message.success(`已保存为第 ${v.version} 版`)
  },
  onError: (e) => message.error(errMsg(e)),
})

/** 去解析：只有存在已保存版本时才出现（保存前没有可解析的文本） */
function goParse() {
  void router.push(`/project/${projectId.value}/parse`)
}

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
      // 回填期间屏蔽 text watch：它的 reset 逻辑针对「用户改正文」，不适用于服务端回填
      hydrating = true
      text.value = sv.text ?? ''
      // 刷新或重新进入页面时恢复已保存版本状态：
      // 否则 savedVersion 恒为 null，「去解析」入口一直不出现。
      if (!savedVersion.value) {
        savedVersion.value = {
          script_version_id: sv.script_version_id,
          version: sv.version ?? 0,
          content_hash: '',
          created_at: '',
        }
        stage.value = 'saved'
      }
      // watcher 默认 pre-flush，会在本轮渲染前执行；nextTick 晚于它，可安全解除屏蔽
      void nextTick(() => {
        hydrating = false
      })
    }
  },
  { immediate: true },
)
</script>

<template>
  <section class="script-page">
    <header class="script-header">
      <div class="header-left">
        <div class="title-block">
          <!-- 左侧「制作流程」栏已高亮 01 剧本。解析独立成 02 后，页内不再有子流程胶囊，
               页头保留一个低权重的步骤标识（14px + 状态点），不恢复 22px 大标题 ——
               那会和左栏「01 剧本」形成同一信息说两遍。 -->
          <h1 class="visually-hidden">剧本</h1>
          <span class="step-title">
            <span class="step-title-no">01</span>
            <span class="step-title-text">剧本</span>
            <span class="step-title-state" :class="{ 'is-done': stage === 'saved' }">
              {{ stage === 'saved' ? '版本已保存' : '编辑中' }}
            </span>
          </span>
        </div>
      </div>
      <div class="header-actions">
        <NButton size="small" quaternary @click="showStylePanel = !showStylePanel">
          {{ showStylePanel ? '收起风格配置' : '创作风格配置' }}
        </NButton>
        <NButton
          size="small"
          :loading="normalizing"
          :disabled="!textValid || normalizing"
          :title="!textValid ? '剧本正文为空或超出字数上限，无法校验' : undefined"
          :aria-label="!textValid ? '校验补充（不可用：剧本正文为空或超出上限）' : '校验补充'"
          @click="handleNormalize"
        >
          {{ normalizing ? '校验中…' : normalizeResult ? '查看校验结果' : '校验补充' }}
        </NButton>
        <NButton
          size="small"
          :type="stage === 'saved' ? 'default' : 'primary'"
          :loading="saving"
          :disabled="!textValid || saving"
          :title="!textValid ? '剧本正文为空或超出字数上限，无法保存版本' : undefined"
          :aria-label="!textValid ? '保存版本（不可用：剧本正文为空或超出上限）' : '保存版本'"
          @click="saveVersionMut()"
        >
          {{ saving ? '保存中…' : savedVersion ? '重新保存' : '保存版本' }}
        </NButton>
        <!-- 去解析：保存后本页唯一的前进出口，主按钮位随之让给它 -->
        <NButton v-if="stage === 'saved'" size="small" type="primary" @click="goParse()">
          去解析 →
        </NButton>
      </div>
    </header>

    <!-- 三步流程指示：剧本编辑 → 内容校验 → 版本已存。
         不再传 hint：原 hint 三句里「编辑后建议先校验」「校验已完成，下一步保存」
         「版本已保存，可以进入解析」都在复述右侧按钮已经说的事（按钮文案本身就是
         下一步动作），且第三句是恒真陈述——保存完成后永远显示，不携带变化信息。 -->
    <ScriptStageBar :steps="steps" :index="stageIndex" :saved-version="savedVersion" />

    <div v-if="showStylePanel" class="style-drawer">
      <StyleConfigPanel :project-id="projectId" />
    </div>
    <!-- 项目默认工作流已移到「设置 → 工作流」，剧本页不再重复承载配置入口 -->
    <div class="script-body">
      <ScriptToolbar
        :view-mode="viewMode"
        :modes="modes"
        :wide="wide"
        :chars="text.length"
        :max-chars="MAX_CHARS"
        @update:view-mode="setMode"
      />

      <div class="panes" :class="`is-${viewMode}`">
        <ScriptEditor
          v-if="viewMode !== 'read'"
          v-model="text"
          placeholder="粘贴或输入 Markdown 剧本原文，也可以只写一个标题或一句话，点『校验补充』由 AI 整理为标准剧本格式…"
        />

        <ScriptReader v-if="viewMode !== 'edit'" :text="text" @edit="setMode('edit')" />
      </div>
    </div>

    <!-- 校验补充：原文与优化稿对比 + 变更清单 + 守恒校验结果 -->
    <ScriptCompareModal
      :show="compareVisible"
      :original-text="text"
      :result="normalizeResult"
      @update:show="(v: boolean) => (compareVisible = v)"
      @adopt="adoptNormalized"
      @keep="keepOriginal"
      @force="forceAdoptNormalized"
    />
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
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.header-left { display: flex; align-items: center; gap: 10px; min-width: 0; }

/* 步骤标识：01 + 名称 + 状态。三档信息按「序号弱 / 名称中 / 状态弱」排布，
   与左栏 active 项构成呼应，但视觉权重明显低于左栏（14px vs 13.5px + 品牌底），
   不会和左栏抢「我在哪一页」这个信号。 */
.title-block { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.step-title { display: inline-flex; align-items: baseline; gap: 8px; min-width: 0; }
.step-title-no {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}
.step-title-text {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}
.step-title-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-tertiary);
}
/* 状态点：未保存为中性，已保存为成功色（形状一致、只换色，因为文字已说明状态） */
.step-title-state::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--neutral-line);
}
.step-title-state.is-done {
  color: var(--state-success);
}
.step-title-state.is-done::before {
  background: var(--state-success);
}

.header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }

.style-drawer {
  flex-shrink: 0;
  max-height: 52vh;
  overflow-y: auto;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
}
.script-body { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.panes { display: flex; flex: 1; min-height: 0; min-width: 0; }
/* 分栏：左编辑右阅读，中间一条竖线分隔（编辑器内部样式随 ScriptEditor） */
.panes.is-split > .editor-pane { flex: 1 1 50%; border-right: 1px solid var(--border-subtle); }
.panes.is-split > .reader { flex: 1 1 50%; }
@media (max-width: 768px) {
  .script-page { height: auto; min-height: calc(100vh - var(--app-header-height, 56px)); }
  /* 竖排：窄屏下标题行和操作行必须分行，横排时操作组最小宽度就有 454px，
     超过 390px 视口，父级 flex-wrap 对「单个超宽 flex 项」不生效 */
  .script-header { flex-direction: column; align-items: stretch; flex-wrap: nowrap; gap: 8px; }
  /* flex-shrink 必须从 0 改成 1、min-width 从 auto 改成 0，
     否则按钮组按内容宽度定宽、拒绝收缩，换行也救不回来 */
  .header-actions {
    flex-shrink: 1;
    min-width: 0;
    flex-wrap: wrap;
    justify-content: flex-start;
  }
}
</style>
