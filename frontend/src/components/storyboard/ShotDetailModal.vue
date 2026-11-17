<script setup lang="ts">
/**
 * 镜头详情弹窗：左预览 + 右三标签页（提示词 / 参考图 / 生成）。
 *
 * 键盘：← / → 切换上一个/下一个镜头，Enter 执行当前标签页的主操作，
 * Esc 关闭，R 重新生成视频。输入框内不拦截 ←/→，避免影响提示词编辑；
 * 输入框内按 Ctrl/Cmd + Enter 同样视为确认。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NModal, NTabPane, NTabs } from 'naive-ui'
import type { Character, Prop, Scene, Shot } from '@/types/script'
import { useShotDetail, type ClipTier } from './useShotDetail'
import ShotPreviewPane from './ShotPreviewPane.vue'
import ShotPromptTab from './ShotPromptTab.vue'
import ShotReferencesTab from './ShotReferencesTab.vue'
import ShotGenerateTab from './ShotGenerateTab.vue'

type TabKey = 'prompt' | 'refs' | 'generate'
type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'

const props = defineProps<{
  show: boolean
  shotId: string | null
  /** 当前集的有序镜头列表：键盘与箭头切换都按这个顺序 */
  shots: Shot[]
  projectId: string
  readinessAll: boolean
  statusKind: StatusKind
  statusLabel: string
  kindLabel: string
  clipJobActive: boolean
  clipWorkflowId?: string
  clipTier: ClipTier
  clipTierOptions: Array<{ label: string; value: string }>
  scenes: Scene[]
  characters: Character[]
  propItems: Prop[]
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'update:shotId': [value: string]
  'update:clipTier': [value: ClipTier]
  /** 生成任务提交成功：页面刷新场景/任务列表并启动进度轮询 */
  submitted: []
  /** 提示词已保存：页面需要重新拉取该镜头（后端做失效传播） */
  'prompts-saved': [shot: Shot]
}>()

const activeIndex = computed(() => props.shots.findIndex((s) => s.id === props.shotId))
const activeShot = computed<Shot | null>(() =>
  activeIndex.value >= 0 ? props.shots[activeIndex.value] : null,
)
const activeSeq = computed(() => (activeIndex.value >= 0 ? activeIndex.value + 1 : 0))
const canPrev = computed(() => activeIndex.value > 0)
const canNext = computed(() => activeIndex.value >= 0 && activeIndex.value < props.shots.length - 1)

const activeTab = ref<TabKey>('generate')

const {
  videoPrompt,
  negativePrompt,
  negativeSupported,
  defaultNegativePrompt,
  savingPrompts,
  refBindingsLoading,
  refBindingsSaving,
  bindings,
  chainFrameStatus,
  unboundSlots,
  assetOptions,
  generating,
  directRegenLoading,
  clipActionIsRegenerate,
  clipActionLabel,
  open,
  savePrompts,
  saveRefBindings,
  addBinding,
  removeBinding,
  setBindingAsset,
  setBindingEnabled,
  handleClipActionClick,
  handleGenerateKeyframe,
  regenerateClipDirect,
} = useShotDetail({
  projectId: () => props.projectId,
  shot: () => activeShot.value,
  scenes: () => props.scenes,
  characters: () => props.characters,
  props: () => props.propItems,
  readinessAll: () => props.readinessAll,
  clipWorkflowId: () => props.clipWorkflowId,
  clipTier: () => props.clipTier,
  setClipTier: (tier: ClipTier) => emit('update:clipTier', tier),
  clipJobActive: () => props.clipJobActive,
  onSubmitted: () => emit('submitted'),
  onPromptsSaved: (shot: Shot) => emit('prompts-saved', shot),
  onClose: () => emit('update:show', false),
})

// 打开或切换镜头时同步提示词并拉取参考图绑定；同镜头的数据刷新不重置用户编辑
watch(
  [() => props.show, () => props.shotId],
  ([show, shotId]) => {
    if (!show || !shotId) return
    const target = props.shots.find((s) => s.id === shotId)
    if (target) void open(target)
  },
  { immediate: true },
)

function navigate(delta: 1 | -1) {
  const next = props.shots[activeIndex.value + delta]
  if (next) emit('update:shotId', next.id)
}

function close() {
  emit('update:show', false)
}

function handleTierChange(value: string) {
  emit('update:clipTier', value as ClipTier)
}

/** Enter：执行当前标签页的主操作（与页面上的主按钮一致） */
function confirmPrimary() {
  if (activeTab.value === 'prompt') {
    void savePrompts()
    return
  }
  if (activeTab.value === 'refs') {
    void saveRefBindings()
    return
  }
  if (props.readinessAll) {
    if (activeShot.value?.keyframe_confirmed) handleClipActionClick()
    else handleGenerateKeyframe()
  }
}

function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || !el.tagName) return false
  const tag = el.tagName.toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable
}

function onKeydown(e: KeyboardEvent) {
  if (!props.show) return
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
    return
  }
  if (isTypingTarget(e.target)) {
    // 文本框内只接受 Ctrl/Cmd + Enter 作为确认，其余按键交给输入法/编辑器
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      confirmPrimary()
    }
    return
  }
  switch (e.key) {
    case 'ArrowLeft':
      if (!canPrev.value) return
      e.preventDefault()
      navigate(-1)
      break
    case 'ArrowRight':
      if (!canNext.value) return
      e.preventDefault()
      navigate(1)
      break
    case 'Enter':
      e.preventDefault()
      confirmPrimary()
      break
    case 'r':
    case 'R':
      e.preventDefault()
      regenerateClipDirect()
      break
    default:
      break
  }
}

watch(
  () => props.show,
  (show) => {
    if (show) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
  { immediate: true },
)

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="镜头详情"
    closable
    :bordered="false"
    class="shot-detail-modal"
    :style="{ width: 'min(1180px, 94vw)' }"
    :content-style="{ maxHeight: 'calc(85vh - 96px)', overflowY: 'auto' }"
    @update:show="emit('update:show', $event)"
  >
    <div v-if="activeShot" class="detail-grid">
      <ShotPreviewPane
        class="detail-preview"
        :shot="activeShot"
        :seq="activeSeq"
        :status-kind="statusKind"
        :status-label="statusLabel"
        :kind-label="kindLabel"
        :can-prev="canPrev"
        :can-next="canNext"
        @navigate="navigate"
      />

      <div class="detail-body">
        <n-tabs v-model:value="activeTab" type="line" size="small" animated>
          <n-tab-pane name="prompt" tab="提示词">
            <ShotPromptTab
              v-model:video-prompt="videoPrompt"
              v-model:negative-prompt="negativePrompt"
              :negative-supported="negativeSupported"
              :default-negative-prompt="defaultNegativePrompt"
              :saving="savingPrompts"
              @save="savePrompts"
            />
          </n-tab-pane>

          <n-tab-pane name="refs" :tab="`参考图（${bindings.length}）`">
            <ShotReferencesTab
              :loading="refBindingsLoading"
              :saving="refBindingsSaving"
              :bindings="bindings"
              :unbound-slots="unboundSlots"
              :asset-options="assetOptions"
              :chain-frame-status="chainFrameStatus"
              @save="saveRefBindings"
              @add="addBinding"
              @remove="removeBinding"
              @update-asset="setBindingAsset"
              @update-enabled="setBindingEnabled"
            />
          </n-tab-pane>

          <n-tab-pane name="generate" tab="生成">
            <ShotGenerateTab
              :tier="clipTier"
              :project-id="projectId"
              :shot-id="activeShot.id"
              :episode="activeShot.episode"
              :readiness-all="readinessAll"
              :keyframe-confirmed="Boolean(activeShot.keyframe_confirmed)"
              :clip-action-label="clipActionLabel"
              :clip-action-is-regenerate="clipActionIsRegenerate"
              :generating="generating"
              :direct-regen-loading="directRegenLoading"
              :tier-options="clipTierOptions"
              @update:tier="handleTierChange"
              @generate-keyframe="handleGenerateKeyframe"
              @clip-action="handleClipActionClick"
            />
          </n-tab-pane>
        </n-tabs>
      </div>
    </div>

    <template #footer>
      <div class="modal-foot">
        <span class="foot-keys">
          <kbd>←</kbd><kbd>→</kbd> 切换镜头
          <kbd>Enter</kbd> 确认
          <kbd>R</kbd> 重新生成
          <kbd>Esc</kbd> 关闭
        </span>
        <n-button size="small" @click="close">关闭</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: minmax(300px, 420px) minmax(0, 1fr);
  gap: var(--sp-6);
  align-items: start;
}
.detail-preview {
  position: sticky;
  top: 0;
}
.detail-body {
  min-width: 0;
}
.modal-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
}
.foot-keys {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  font-size: 12px;
  color: var(--text-tertiary);
}
kbd {
  padding: 0 5px;
  margin-right: 2px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 17px;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
}
/* 弹窗标题接入页面衬线标题体系 */
:deep(.n-card-header__main) {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
@media (max-width: 768px) {
  .detail-grid {
    grid-template-columns: 1fr;
    gap: var(--sp-4);
  }
  .detail-preview {
    position: static;
  }
  .modal-foot {
    flex-wrap: wrap;
  }
}
</style>
