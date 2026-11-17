<script setup lang="ts">
/**
 * 分镜图详情弹窗：左边大图，右边三段（参考图 → 重新生成工作台 → 生成操作）。
 * 状态、槽位推导与提交都在 useKeyframeRegen 里，本组件只做组织与展示。
 */
import { computed } from 'vue'
import { NButton, NImage, NInput, NModal, NSpace, NSpin, NTag, NUpload, useMessage } from 'naive-ui'
import type { Shot } from '@/types/script'
import { useKeyframeRegen } from '@/composables/useKeyframeRegen'
import { shotStatus } from './types'
import KeyframeRefList from './KeyframeRefList.vue'
import RegenWorkbench from './RegenWorkbench.vue'

const props = withDefaults(
  defineProps<{
    projectId: string
    reviewApproved: boolean
    scenes: Array<{ id: string; title: string; baseline_asset_id?: string | null }>
    /** 全片镜号 */
    seqOf: (shot: Shot) => number
  }>(),
  {},
)

const emit = defineEmits<{
  /** 提交成功后：页面建立轮询基线 */
  submitted: []
  /** 尚未出图的镜头：走原生成链路（页面处理） */
  'first-generate': [shot: Shot, prompt: string]
}>()

const message = useMessage()

const {
  detailVisible,
  detailShot,
  openDetail,
  kfSlots,
  kfMaxRefs,
  kfRefsLoading,
  kfRefList,
  regenPrompt,
  regenMode,
  regenModeOptions,
  regenRefs,
  regenRefRows,
  regenRefsEditable,
  regenValidation,
  regenSubmitting,
  regenValidating,
  removeRegenRef,
  runRegenValidation,
  detailGenerate,
  assetPickerVisible,
  assetPickerSlot,
  assetPickerKeyword,
  assetPickerLoading,
  assetPickerItems,
  assetUploading,
  openAssetPicker,
  loadAssetPickerItems,
  pickAsset,
  uploadRefImage,
} = useKeyframeRegen({
  projectId: computed(() => props.projectId),
  reviewApproved: computed(() => props.reviewApproved),
  scenes: computed(() => props.scenes),
  message,
  onSubmitted: () => emit('submitted'),
  onFirstGenerate: (shot, prompt) => emit('first-generate', shot, prompt),
})

defineExpose({ open: openDetail })

const heroUrl = computed(() => {
  const kf = detailShot.value?.current_assets?.keyframe
  return kf?.url || kf?.thumb_url || ''
})
const heroTitle = computed(() => {
  const shot = detailShot.value
  if (!shot) return ''
  return shot.title || `镜头 ${props.seqOf(shot)}`
})
const heroStatus = computed(() => (detailShot.value ? shotStatus(detailShot.value).label : ''))
const boundCount = computed(() => regenRefs.value.length)
</script>

<template>
  <n-modal
    v-model:show="detailVisible"
    preset="card"
    title="分镜图详情"
    closable
    :bordered="false"
    :style="{ width: 'min(1120px, 93vw)' }"
    :content-style="{ maxHeight: 'calc(85vh - 56px)', overflowY: 'auto' }"
  >
    <template v-if="detailShot">
      <div class="detail-grid">
        <div class="detail-hero">
          <n-image
            v-if="heroUrl"
            :src="heroUrl"
            :preview-src="heroUrl"
            class="detail-img"
            object-fit="contain"
            alt="分镜图"
          />
          <div v-else class="detail-empty">该镜头尚未生成分镜图</div>
          <div class="detail-meta">
            <span class="detail-name">{{ heroTitle }}</span>
            <span class="detail-status">{{ heroStatus }}</span>
            <n-tag v-if="detailShot.current_assets?.keyframe?.locked" size="tiny" type="warning">已锁定</n-tag>
            <n-tag v-if="detailShot.keyframe_confirmed" size="tiny" type="success">已确认</n-tag>
          </div>
        </div>

        <div class="detail-body">
          <section class="detail-section">
            <div class="detail-section-head">
              <span class="detail-section-index">1</span>
              <span class="detail-section-name">生成参考图</span>
              <span class="detail-section-hint">当前生图工作流 {{ kfMaxRefs }} 个参考槽位</span>
            </div>
            <KeyframeRefList :items="kfRefList" :loading="kfRefsLoading" :max-refs="kfMaxRefs" />
            <div class="detail-source-hint">
              真实入参＝实际提交的图；按槽位推导＝按引用与槽位会传入；无可用槽位＝本次不传；图未生成＝需先到资产库补齐。
            </div>
          </section>

          <section class="detail-section">
            <div class="detail-section-head">
              <span class="detail-section-index">2</span>
              <span class="detail-section-name">重新生成工作台</span>
            </div>
            <RegenWorkbench
              v-model:prompt="regenPrompt"
              v-model:mode="regenMode"
              :mode-options="regenModeOptions"
              :rows="regenRefRows"
              :slots-count="kfSlots.length"
              :bound-count="boundCount"
              :editable="regenRefsEditable"
              :validation="regenValidation"
              @pick="openAssetPicker"
              @remove="removeRegenRef"
            />
          </section>

          <section class="detail-section">
            <div class="detail-section-head">
              <span class="detail-section-index">3</span>
              <span class="detail-section-name">生成操作</span>
            </div>
            <div v-if="!reviewApproved" class="detail-gate">公共资产尚未通过人工审核，暂不可生成分镜图。</div>
            <div class="detail-actions">
              <n-button v-if="detailShot.current_assets?.keyframe" :loading="regenValidating" @click="runRegenValidation">
                校验
              </n-button>
              <n-button
                type="primary"
                :disabled="!reviewApproved || !!detailShot.current_assets?.keyframe?.locked"
                :loading="regenSubmitting"
                @click="detailGenerate(detailShot)"
              >
                {{ detailShot.current_assets?.keyframe ? '重新生成分镜图' : '生成分镜图' }}
              </n-button>
            </div>
          </section>
        </div>
      </div>
    </template>
  </n-modal>

  <!-- 资产选择器：为参考图槽位选择或上传参考图 -->
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
        @update:value="() => loadAssetPickerItems()"
      />
      <n-spin :show="assetPickerLoading || assetUploading">
        <div v-if="assetPickerItems.length" class="picker-list">
          <div v-for="a in assetPickerItems" :key="a.id" class="picker-item" @click="pickAsset(a)">
            <n-image
              :src="`/api/assets/${a.id}/file`"
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
/* 居中大弹窗：左图右文，窄屏回落单列 */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(280px, 420px) minmax(0, 1fr);
  gap: var(--sp-6);
  align-items: start;
}
.detail-body {
  min-width: 0;
}
.detail-img {
  width: 100%;
  border-radius: var(--radius-lg);
  background: var(--bg-sunken);
  display: block;
}
.detail-img :deep(img) {
  width: 100%;
  max-height: 480px;
  object-fit: contain;
  display: block;
}
.detail-empty {
  padding: var(--sp-8) var(--sp-4);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-lg);
  text-align: center;
  font-size: 13px;
  color: var(--text-secondary);
  background: var(--bg-elevated);
}
.detail-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
  flex-wrap: wrap;
}
.detail-name {
  font-size: 14px;
  font-weight: 600;
}
.detail-status {
  font-size: 12px;
  color: var(--text-tertiary);
}
.detail-section {
  padding: var(--sp-4) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.detail-section:last-of-type {
  border-bottom: none;
  padding-bottom: var(--sp-1);
}
.detail-section-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}
.detail-section-index {
  width: 18px;
  height: 18px;
  border-radius: var(--radius-sm);
  background: var(--brand-deep);
  color: var(--text-on-brand);
  font-size: 11px;
  line-height: 18px;
  text-align: center;
  flex-shrink: 0;
}
.detail-section-name {
  font-size: 14px;
  font-weight: 600;
}
.detail-section-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
}
.detail-source-hint {
  margin-top: var(--sp-2);
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.detail-gate {
  margin-bottom: var(--sp-2);
  padding: var(--sp-2) var(--sp-2);
  border-radius: var(--radius-lg);
  background: var(--warning-soft);
  font-size: 12px;
  color: var(--accent-amber);
}
.detail-actions {
  display: flex;
  gap: var(--sp-2);
}
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
  border-color: var(--brand-primary);
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
@media (max-width: 980px) {
  .detail-grid {
    grid-template-columns: 1fr;
    gap: var(--sp-4);
  }
}
@media (min-width: 981px) {
  .detail-hero {
    position: sticky;
    top: 0;
  }
}
@media (max-width: 768px) {
  .detail-section {
    padding: var(--sp-3) 0;
  }
  .detail-section-name {
    font-size: 13px;
  }
  .detail-actions :deep(.n-button) {
    flex: 1;
  }
}
:deep(.n-card-header__main) {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
</style>
