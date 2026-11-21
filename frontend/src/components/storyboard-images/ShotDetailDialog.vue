<script setup lang="ts">
/**
 * 分镜图详情弹窗：左栏大图 + 右栏编辑区（提示词 / 参考图 / 重新生成）。
 *
 * 重新生成按钮直接按当前页面的提示词与参考图提交（modify_refs 模式），
 * 新分镜图生成后原地覆盖旧版，只保留一份。
 */
import { computed, ref } from 'vue'
import {
  NButton,
  NImage,
  NInput,
  NModal,
  NSpace,
  NSpin,
  NUpload,
  useMessage,
} from 'naive-ui'
import type { Shot } from '@/types/script'
import { useKeyframeRegen } from '@/composables/useKeyframeRegen'
import type { Asset } from '@/types/asset'
import { listAssets, uploadAsset } from '@/api/assets'

const props = defineProps<{
  projectId: string
  scenes: Array<{ id: string; title: string; baseline_asset_id?: string | null }>
  /** 全片镜号 */
  seqOf: (shot: Shot) => number
}>()

const emit = defineEmits<{
  submitted: []
  'first-generate': [shot: Shot, prompt: string]
  replace: [shot: Shot]
}>()

const message = useMessage()

const {
  detailVisible,
  detailShot,
  openDetail,
  kfSlots,
  kfMaxRefs,
  kfRefsLoading,
  regenPrompt,
  regenRefs,
  regenSubmitting,
  pickAsset,
  removeRegenRef,
  detailGenerate,
} = useKeyframeRegen({
  projectId: computed(() => props.projectId),
  scenes: computed(() => props.scenes),
  message,
  onSubmitted: () => emit('submitted'),
  onFirstGenerate: (shot, prompt) => emit('first-generate', shot, prompt),
})

defineExpose({ open: openDetail, visible: detailVisible })

const heroUrl = computed(() => {
  const kf = detailShot.value?.current_assets?.keyframe
  return kf?.url || kf?.thumb_url || ''
})
const hasKeyframe = computed(() => Boolean(detailShot.value?.current_assets?.keyframe))

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

async function loadPickerItems() {
  assetPickerLoading.value = true
  try {
    const res = await listAssets({
      project_id: props.projectId,
      asset_type: undefined,
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
  pickAsset(asset)
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

// ── 参考图展示（合并已绑定与可用槽位）──
const refRows = computed(() => {
  const bound = new Set(regenRefs.value.map((r) => r.slot_param))
  const rows: Array<{
    slot_param: string
    asset_id: string | null
    asset_type?: string
    asset_name?: string
    version?: number
    required: boolean
    enabled: boolean
  }> = []
  for (const r of regenRefs.value) {
    rows.push({
      slot_param: r.slot_param,
      asset_id: r.asset_id ?? null,
      asset_type: r.asset_type ?? undefined,
      asset_name: r.asset_name ?? undefined,
      version: r.asset_version ?? undefined,
      required: r.required ?? false,
      enabled: r.enabled !== false,
    })
  }
  for (const s of kfSlots.value) {
    if (bound.has(s.param)) continue
    rows.push({
      slot_param: s.param,
      asset_id: null,
      required: s.required,
      enabled: false,
    })
  }
  return rows
})

</script>

<template>
  <n-modal
    v-model:show="detailVisible"
    preset="card"
    title="分镜图详情"
    closable
    :bordered="false"
    :style="{ width: 'min(1100px, 94vw)' }"
    :content-style="{ maxHeight: 'calc(88vh - 56px)', overflowY: 'auto' }"
  >
    <template v-if="detailShot">
      <div class="dg">
        <!-- 左栏：大图预览 -->
        <aside class="dg-aside">
          <n-image v-if="heroUrl" :src="heroUrl" object-fit="contain" class="dg-img" />
          <div v-else class="dg-empty">暂无预览</div>
        </aside>

        <!-- 右栏：编辑区 -->
        <div class="dg-main">
          <!-- 提示词 -->
          <div class="dg-sec">
            <div class="dg-label">提示词</div>
            <n-input
              v-model:value="regenPrompt"
              type="textarea"
              :autosize="{ minRows: 4, maxRows: 12 }"
              placeholder="描述该镜头的画面要求…"
            />
          </div>

          <!-- 参考图 -->
          <div class="dg-sec">
            <div class="dg-label">
              参考图
              <span v-if="kfMaxRefs" class="dg-hint">（槽位上限 {{ kfMaxRefs }}）</span>
            </div>
            <n-spin :show="kfRefsLoading" size="small">
              <div v-if="refRows.length" class="rf-grid">
                <div v-for="row in refRows" :key="row.slot_param" class="rf-card">
                  <div class="rf-preview">
                    <img
                      v-if="row.asset_id"
                      :src="`/api/assets/${row.asset_id}/file?thumb=true`"
                      :alt="row.asset_name || row.slot_param"
                    />
                    <div v-else class="rf-empty">未绑定</div>
                  </div>
                  <div class="rf-meta">
                    <span class="rf-slot">{{ row.slot_param }}<span v-if="row.required" class="rf-required">*</span></span>
                    <span v-if="row.asset_name" class="rf-name">{{ row.asset_name }}</span>
                  </div>
                  <div class="rf-actions">
                    <n-button size="tiny" @click="openPicker(row.slot_param)">
                      {{ row.asset_id ? '替换' : '添加' }}
                    </n-button>
                    <n-button
                      v-if="row.asset_id && !row.required"
                      size="tiny"
                      @click="removeRegenRef(row.slot_param)"
                    >
                      移除
                    </n-button>
                  </div>
                </div>
              </div>
              <div v-else class="rf-none">暂无参考图槽位</div>
            </n-spin>
          </div>

          <!-- 重新生成按钮 -->
          <div class="dg-sec is-last">
            <n-button
              type="primary"
              :loading="regenSubmitting"
              :disabled="!hasKeyframe"
              @click="detailShot && detailGenerate(detailShot)"
            >
              重新生成分镜图
            </n-button>
            <span v-if="!hasKeyframe" class="dg-note">尚未生成分镜图，请先使用左侧按钮生成</span>
          </div>
        </div>
      </div>
    </template>
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
  display: grid;
  grid-template-columns: minmax(260px, 380px) minmax(0, 1fr);
  gap: var(--sp-6);
  align-items: start;
}
/* 左栏：分镜图 + sticky 吸附。
 *
 * sticky（2026-11-21）：弹窗内容区整块滚动（实测内容 1137px vs 可视 620px），
 * 右栏有提示词 + 9 个参考槽位 + 重新生成按钮，很长。若不吸附，
 * 用户往下滚看参考图时左侧分镜图会滚出视野 —— 而「看着参考图调提示词」
 * 正是这个弹窗的核心动作。让左栏吸附在内容区顶部，滚动时图始终可见。
 */
.dg-aside {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  position: sticky;
  top: 0;
  align-self: start;
}
/* 大图预览：必须同时约束「容器」和「容器内的 img」。
 *
 * 事故（2026-11-21）：这里原本只写了容器的 `max-height: 380px`，
 * 而 Naive UI 的 n-image 内部 <img> 是独立元素，不吃容器的 max-height。
 * 实测：容器 380×380，内部 img 却渲染成 768×1344 —— 图片直接溢出容器，
 * 向右延伸到 x=926，把右栏的提示词输入框、参考图卡片整片盖住。
 * 用户看到的就是「大图把提示词和参考图遮住了」。
 *
 * 约束手段：
 *   max-width / max-height 限死外框尺寸
 *   object-fit: contain  保证不裁切、不变形
 *   内层 img 同样限死 100%，否则它按固有尺寸渲染
 *
 * sticky（2026-11-21 补充）：弹窗内容区是整块滚动（实测内容 1137px vs
 * 可视 620px），右栏有提示词 + 9 个参考槽位 + 重新生成按钮，很长。
 * 若不吸附，用户往下滚看参考图时，左侧分镜图会滚出视野 ——
 * 而「看着参考图调提示词」正是这个弹窗的核心动作。
 * 让左栏 sticky 在内容区顶部，滚动时图始终可见。
 */
/* 大图预览：容器按图片真实比例伸缩，不再锁死正方形。
 *
 * 事故（2026-11-21）：这里原本只写了容器的 `max-height: 380px`，
 * 而 Naive UI 的 n-image 内部 <img> 是独立元素，不吃容器的 max-height。
 * 实测：容器 380×380，内部 img 却渲染成 768×1344 —— 图片溢出容器，
 * 向右延伸到 x=926，把右栏提示词输入框、参考图卡片整片盖住。
 *
 * 第二层问题（同日）：修好溢出后，容器仍是固定 380×380 的正方形。
 * 该镜头资产是 768×1344 竖图（比例 0.571），塞进 1:1 的框里 contain 后
 * 左右各留 100px 黑边，实测渲染比例 1.000 ≠ 真实比例 0.571，
 * 视觉上像「被压扁的截图」。
 *
 * 最终方案：容器不给固定高度，改用 `height: auto` + 内层 img 自然撑开。
 * Naive UI 会给 .n-image 和 img 都算出 380px 高度（不是内联样式，是它自己的
 * CSS），普通声明压不过，必须 !important。 */
.dg-img {
  width: 100%;
  display: block;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--media-backdrop);
}
/* 外层容器（.n-image）：解除固定高度，让图片决定 */
.dg-img.n-image {
  height: auto !important;
  max-height: none !important;
}
/* 内层 img：按宽度铺满、高度按比例自适应，仅用 max-height 兜底 */
.dg-img :deep(img) {
  display: block;
  width: 100% !important;
  max-width: 100%;
  height: auto !important;
  max-height: 380px;
  object-fit: contain;
  margin: 0 auto;
}
.dg-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 200px;
  font-size: 13px;
  color: var(--text-tertiary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.dg-info {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.dg-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.dg-quick {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
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
.dg-note {
  font-size: 12px;
  color: var(--text-tertiary);
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
  flex-direction: column;
  gap: 2px;
}
.rf-slot {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
}
.rf-required {
  color: var(--state-error);
  margin-left: 2px;
}
.rf-name {
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rf-actions {
  flex: none;
  display: flex;
  gap: var(--sp-1);
}
.rf-none {
  font-size: 12px;
  color: var(--text-tertiary);
  padding: var(--sp-4) 0;
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

@media (max-width: 768px) {
  .dg {
    grid-template-columns: 1fr;
    gap: var(--sp-4);
  }
}
@media (min-width: 769px) {
  .dg-aside {
    position: sticky;
    top: 0;
  }
}
</style>
