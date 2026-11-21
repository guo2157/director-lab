<script setup lang="ts">
/**
 * 镜头详情弹窗（分镜看板）：扁平布局。
 *
 * 布局：镜头视频 → 可编辑提示词 → 参考图管理（新增/替换/移除）→ 重新生成按钮。
 * 重新生成按当前提示词、参考图与对应角色音色提交，新视频生成后原地覆盖旧版，只保留一份。
 *
 * 键盘：← / → 切换上一个/下一个镜头，Esc 关闭。输入框内不拦截 ←/→。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { NButton, NInput, NModal, NSpin, NUpload, useMessage } from 'naive-ui'
import type { Character, Prop, Scene, Shot } from '@/types/script'
import { useShotDetail } from './useShotDetail'
import type { Asset } from '@/types/asset'
import { listAssets, uploadAsset } from '@/api/assets'

type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'

const props = defineProps<{
  show: boolean
  shotId: string | null
  shots: Shot[]
  projectId: string
  readinessAll: boolean
  statusKind: StatusKind
  statusLabel: string
  kindLabel: string
  clipJobActive: boolean
  clipWorkflowId?: string
  scenes: Scene[]
  characters: Character[]
  propItems: Prop[]
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'update:shotId': [value: string]
  submitted: []
  'prompts-saved': [shot: Shot]
}>()

const message = useMessage()

const activeIndex = computed(() => props.shots.findIndex((s) => s.id === props.shotId))
const activeShot = computed<Shot | null>(() =>
  activeIndex.value >= 0 ? props.shots[activeIndex.value] : null,
)
const canPrev = computed(() => activeIndex.value > 0)
const canNext = computed(() => activeIndex.value >= 0 && activeIndex.value < props.shots.length - 1)

const {
  videoPrompt,
  negativePrompt,
  negativeSupported,
  defaultNegativePrompt,
  refBindingsLoading,
  bindings,
  unboundSlots,
  generating,
  clipActionLabel,
  open,
  addBinding,
  removeBinding,
  setBindingAsset,
  handleClipActionClick,
  regenerateClipDirect,
} = useShotDetail({
  projectId: () => props.projectId,
  shot: () => activeShot.value,
  scenes: () => props.scenes,
  characters: () => props.characters,
  props: () => props.propItems,
  readinessAll: () => props.readinessAll,
  clipWorkflowId: () => props.clipWorkflowId,
  clipJobActive: () => props.clipJobActive,
  onSubmitted: () => emit('submitted'),
  onPromptsSaved: (shot: Shot) => emit('prompts-saved', shot),
  onClose: () => emit('update:show', false),
})

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
  if (isTypingTarget(e.target)) return
  if (e.key === 'ArrowLeft' && canPrev.value) { e.preventDefault(); navigate(-1) }
  if (e.key === 'ArrowRight' && canNext.value) { e.preventDefault(); navigate(1) }
  if (e.key === 'r' || e.key === 'R') { e.preventDefault(); regenerateClipDirect() }
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

// ── 资产选择器（为参考图槽位选择已有资产）──
const assetPickerVisible = ref(false)
const assetPickerSlot = ref('')
const assetPickerKeyword = ref('')
const assetPickerLoading = ref(false)
const assetPickerItems = ref<Asset[]>([])
const assetUploading = ref(false)

function openPicker(slotParam: string) {
  assetPickerSlot.value = slotParam
  assetPickerKeyword.value = ''
  assetPickerVisible.value = true
  void loadPickerItems()
}

/** 已有绑定的 reference_id，用于 setBindingAsset */
function refIdForSlot(slotParam: string): string {
  return bindings.value.find((b) => b.slot_id === slotParam)?.reference_id ?? slotParam
}

async function loadPickerItems() {
  assetPickerLoading.value = true
  try {
    const res = await listAssets({
      project_id: props.projectId,
      keyword: assetPickerKeyword.value || undefined,
      page_size: 50,
    })
    assetPickerItems.value = (res.items ?? []) as Asset[]
  } catch (e: any) {
    message.error(e?.message ?? '加载资产列表失败')
    assetPickerItems.value = []
  } finally {
    assetPickerLoading.value = false
  }
}

function pickAndClose(asset: Asset) {
  const slotParam = assetPickerSlot.value
  if (!slotParam) return
  const refId = refIdForSlot(slotParam)
  const exists = bindings.value.some((b) => b.slot_id === slotParam)
  if (!exists) {
    addBinding(slotParam)
  }
  setBindingAsset(refId, asset.id)
  assetPickerVisible.value = false
}

async function uploadRefImage(file: File) {
  const slotParam = assetPickerSlot.value
  if (!slotParam) return
  assetUploading.value = true
  try {
    const asset = await uploadAsset(file, {
      project_id: props.projectId,
      asset_type: 'reference',
      name: file.name.replace(/\.[^.]+$/, ''),
    })
    pickAndClose(asset)
  } catch (e: any) {
    message.error(e?.message ?? '上传参考图失败')
  } finally {
    assetUploading.value = false
  }
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="镜头详情"
    closable
    :bordered="false"
    class="shot-detail-modal"
    :style="{ width: 'min(1100px, 94vw)' }"
    :content-style="{ maxHeight: 'calc(85vh - 96px)', overflowY: 'auto' }"
    @update:show="emit('update:show', $event)"
  >
    <div v-if="activeShot" class="dg">
      <div class="dg-main">
        <!-- 镜头视频 -->
        <div class="dg-sec">
          <div class="dg-label">镜头视频</div>
          <div v-if="activeShot.current_assets?.clip" class="dg-video">
            <video
              v-if="activeShot.current_assets.clip.url"
              :src="activeShot.current_assets.clip.url"
              controls
              preload="metadata"
              class="dg-video-el"
            />
            <div v-else class="dg-empty">视频文件不存在</div>
          </div>
          <div v-else class="dg-empty">尚未生成视频</div>
        </div>

        <!-- 提示词 -->
        <div class="dg-sec">
          <div class="dg-label">提示词</div>
          <n-input
            v-model:value="videoPrompt"
            type="textarea"
            :autosize="{ minRows: 4, maxRows: 12 }"
            placeholder="描述该镜头的视频画面要求…"
          />
          <template v-if="negativeSupported">
            <div class="dg-label" style="margin-top: var(--sp-2)">负面提示词</div>
            <n-input
              v-model:value="negativePrompt"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 6 }"
              :placeholder="defaultNegativePrompt || '不希望出现的元素'"
            />
          </template>

        </div>

        <!-- 参考图 -->
        <div class="dg-sec">
          <div class="dg-label">
            参考图
            <span class="dg-hint">（{{ bindings.length }} 张）</span>
          </div>
          <n-spin :show="refBindingsLoading" size="small">
            <div v-if="bindings.length" class="rf-grid">
              <div v-for="b in bindings" :key="b.reference_id" class="rf-card">
                <div class="rf-preview">
                  <img
                    v-if="b.asset_id"
                    :src="`/api/assets/${b.asset_id}/file?thumb=true`"
                    :alt="b.slot_id"
                  />
                  <div v-else class="rf-empty">未绑定</div>
                </div>
                <div class="rf-meta">
                  <!-- 图号 = 模型实际收到的顺序 = 提示词里的 <Picture N>。
                       不显示用户就无法把页面槽位与提示词编号对照（实测遗漏后无从判断哪张是 Picture 1）。 -->
                  <span v-if="b.image_no != null" class="rf-no">图{{ b.image_no }}</span>
                  <span class="rf-slot">{{ b.slot_id }}</span>
                </div>
                <div class="rf-actions">
                  <n-button size="tiny" @click="openPicker(b.slot_id)">
                    {{ b.asset_id ? '替换' : '添加' }}
                  </n-button>
                  <n-button size="tiny" @click="removeBinding(b.reference_id)">移除</n-button>
                </div>
              </div>
            </div>
            <div v-else class="rf-none">暂无参考图绑定</div>
          </n-spin>

          <!-- 未绑定槽位 -->
          <template v-if="unboundSlots.length">
            <div class="dg-label" style="margin-top: var(--sp-3)">可用槽位</div>
            <div class="rf-grid">
              <div v-for="s in unboundSlots" :key="s.param" class="rf-card">
                <div class="rf-preview"><div class="rf-empty">空</div></div>
                <div class="rf-meta">
                  <span class="rf-slot">{{ s.param }}<span v-if="s.required" class="rf-required">*</span></span>
                </div>
                <div class="rf-actions">
                  <n-button size="tiny" @click="openPicker(s.param)">添加</n-button>
                </div>
              </div>
            </div>
          </template>


        </div>

        <!-- 重新生成按钮 -->
        <div class="dg-sec is-last">
          <n-button
            type="primary"
            :loading="generating"
            :disabled="!props.readinessAll"
            @click="handleClipActionClick"
          >
            {{ clipActionLabel }}
          </n-button>
          <span v-if="!props.readinessAll" class="dg-note">资产库未就绪，请先完成必需资产的生成</span>
        </div>
      </div>
    </div>

  </n-modal>

  <!-- 资产选择器 -->
  <n-modal
    v-model:show="assetPickerVisible"
    preset="card"
    :title="`选择参考图（槽位 ${assetPickerSlot}）`"
    closable
    :bordered="false"
    :style="{ width: 'min(560px, 92vw)' }"
  >
    <n-space vertical>
      <n-input
        v-model:value="assetPickerKeyword"
        placeholder="搜索资产名称"
        clearable
        size="small"
        @update:value="loadPickerItems"
      />
      <n-spin :show="assetPickerLoading || assetUploading">
        <div v-if="assetPickerItems.length" class="picker-list">
          <div
            v-for="a in assetPickerItems"
            :key="a.id"
            class="picker-item"
            @click="pickAndClose(a)"
          >
            <n-image
              :src="`/api/assets/${a.id}/file?thumb=true`"
              :width="44"
              :height="44"
              object-fit="cover"
              :img-props="{ style: 'width:44px;height:44px;object-fit:cover;border-radius:var(--radius)' }"
            />
            <div class="picker-meta">
              <div class="picker-name">{{ a.name || a.id }}</div>
              <div class="picker-sub">{{ a.asset_type }} · v{{ a.version }}</div>
            </div>
          </div>
        </div>
        <div v-else class="picker-empty">没有匹配的资产</div>
      </n-spin>
      <n-upload
        :show-file-list="false"
        accept="image/*"
        :custom-request="(opt: any) => uploadRefImage(opt.file.file as File)"
      >
        <n-button size="small" secondary :loading="assetUploading">本地上传参考图</n-button>
      </n-upload>
    </n-space>
  </n-modal>
</template>

<style scoped>
.dg {
  display: flex;
  flex-direction: column;
}
.dg-main {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  min-width: 0;
}
.dg-sec {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: var(--sp-3) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.dg-sec:first-child {
  padding-top: 0;
}
.dg-sec.is-last {
  border-bottom: none;
  padding-bottom: 0;
  align-items: flex-start;
}
.dg-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.dg-hint {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-tertiary);
}
.dg-row {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
  margin-top: var(--sp-1);
}
.dg-note {
  font-size: 12px;
  color: var(--text-tertiary);
}
.dg-video {
  width: 100%;
}
.dg-video-el {
  width: 100%;
  max-height: 360px;
  border-radius: var(--radius-lg);
  /* 视频 letterbox 黑场：DESIGN.md 规定这是唯一允许的纯黑，必须走令牌 */
  background: var(--media-backdrop);
}
.dg-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 120px;
  font-size: 13px;
  color: var(--text-tertiary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}

/* 参考图 */
.rf-grid {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.rf-card {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.rf-preview {
  flex: none;
  width: 48px;
  height: 48px;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--bg-sunken);
}
.rf-preview img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.rf-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  font-size: 10px;
  color: var(--text-tertiary);
}
.rf-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 6px;
}
/* 图号是用户对照提示词 <Picture N> 的唯一锚点：字号、字重、颜色三个维度都要压过槽位名，
   否则槽位名（长英文键名）在视觉上更抢眼，图号被当成装饰忽略。 */
.rf-no {
  font-family: var(--font-mono);
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  flex-shrink: 0;
}
.rf-slot {
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 400;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rf-required {
  color: var(--state-error);
  margin-left: 2px;
}
.rf-actions {
  flex: none;
  display: flex;
  gap: var(--sp-1);
}
.rf-none {
  font-size: 12px;
  color: var(--text-tertiary);
  padding: var(--sp-3) 0;
  text-align: center;
}

/* 资产选择器 */
.picker-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  max-height: 320px;
  overflow-y: auto;
}
.picker-item {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-1) var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);
}
.picker-item:hover {
  border-color: var(--brand-line);
  background: var(--bg-elevated);
}
.picker-meta {
  flex: 1;
  min-width: 0;
}
.picker-name {
  font-size: 13px;
}
.picker-sub {
  font-size: 11px;
  color: var(--text-tertiary);
}
.picker-empty {
  padding: var(--sp-6) 0;
  text-align: center;
  font-size: 13px;
  color: var(--text-tertiary);
}

:deep(.n-card-header__main) {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.01em;
}

</style>
