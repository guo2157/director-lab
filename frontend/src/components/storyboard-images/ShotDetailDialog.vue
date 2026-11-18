<script setup lang="ts">
/**
 * 分镜图详情弹窗：左栏大图 + 常驻主操作，右栏三段（提示词 → 参考图 → 重新生成方式）。
 * 状态、槽位推导与提交都在 useKeyframeRegen 里，本组件只做组织与展示。
 *
 * 布局原则（DESIGN.md「主操作不进折叠」）：
 *   · 「重新生成 / 替换上传」贴在图上，滚到任何位置都能点到；
 *   · 提示词按段落完整展示，不塞进 textarea 也不折成摘要，要改才进编辑态。
 */
import { computed } from 'vue'
import { NButton, NImage, NInput, NModal, NSpace, NSpin, NUpload, useMessage } from 'naive-ui'
import type { Shot } from '@/types/script'
import { useKeyframeRegen } from '@/composables/useKeyframeRegen'
import { shotStatus } from './types'
import KeyframeRefList from './KeyframeRefList.vue'
import PromptPanel from './PromptPanel.vue'
import RegenWorkbench from './RegenWorkbench.vue'
import ShotPreviewPanel from './ShotPreviewPanel.vue'

const props = defineProps<{
  projectId: string
  reviewApproved: boolean
  scenes: Array<{ id: string; title: string; baseline_asset_id?: string | null }>
  /** 全片镜号 */
  seqOf: (shot: Shot) => number
}>()

const emit = defineEmits<{
  /** 提交成功后：页面建立轮询基线 */
  submitted: []
  /** 尚未出图的镜头：走原生成链路（页面处理） */
  'first-generate': [shot: Shot, prompt: string]
  /** 替换上传：交给页面弹本地文件选择（保持卡片与详情同一套逻辑） */
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
  regenWorkflowId,
  workflowSchema,
  regenParams,
} = useKeyframeRegen({
  projectId: computed(() => props.projectId),
  reviewApproved: computed(() => props.reviewApproved),
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
const heroTitle = computed(() => {
  const shot = detailShot.value
  if (!shot) return ''
  return shot.title || `镜头 ${props.seqOf(shot)}`
})
const heroSeq = computed(() => (detailShot.value ? props.seqOf(detailShot.value) : 0))
const heroStatus = computed(() => {
  const shot = detailShot.value
  return shot ? shotStatus(shot) : { kind: 'stale' as const, label: '待生成' }
})
const locked = computed(() => Boolean(detailShot.value?.current_assets?.keyframe?.locked))
const confirmed = computed(() => Boolean(detailShot.value?.keyframe_confirmed))
const hasKeyframe = computed(() => Boolean(detailShot.value?.current_assets?.keyframe))
const boundCount = computed(() => regenRefs.value.length)

function onReplace() {
  if (detailShot.value) emit('replace', detailShot.value)
}
</script>

<template>
  <n-modal
    v-model:show="detailVisible"
    preset="card"
    title="分镜图详情"
    closable
    :bordered="false"
    :style="{ width: 'min(1180px, 94vw)' }"
    :content-style="{ maxHeight: 'calc(88vh - 56px)', overflowY: 'auto' }"
  >
    <template v-if="detailShot">
      <div class="dg">
        <aside class="dg-aside">
          <ShotPreviewPanel
            :url="heroUrl"
            :title="heroTitle"
            :seq="heroSeq"
            :status-kind="heroStatus.kind"
            :status-label="heroStatus.label"
            :has-keyframe="hasKeyframe"
            :locked="locked"
            :confirmed="confirmed"
            :can-generate="reviewApproved"
            :generating="regenSubmitting"
            :validating="regenValidating"
            @generate="detailGenerate(detailShot)"
            @replace="onReplace"
            @validate="runRegenValidation"
          />
        </aside>

        <div class="dg-main">
          <section class="dg-sec">
            <header class="dg-sec-head">
              <span class="dg-sec-idx">1</span>
              <span class="dg-sec-name">提示词</span>
              <span class="dg-sec-hint">改动只影响本镜</span>
            </header>
            <PromptPanel v-model="regenPrompt" @copied="message.success('已复制提示词')" />
          </section>

          <section class="dg-sec">
            <header class="dg-sec-head">
              <span class="dg-sec-idx">2</span>
              <span class="dg-sec-name">生成参考图</span>
              <span class="dg-sec-hint">当前工作流 {{ kfMaxRefs }} 个槽位</span>
            </header>
            <KeyframeRefList :items="kfRefList" :loading="kfRefsLoading" :max-refs="kfMaxRefs" />
            <p class="dg-note">
              真实入参＝实际提交的图；按槽位推导＝按引用与槽位会传入；无可用槽位＝本次不传；图未生成＝需先到资产库补齐。
            </p>
          </section>

          <section class="dg-sec is-last">
            <header class="dg-sec-head">
              <span class="dg-sec-idx">3</span>
              <span class="dg-sec-name">重新生成方式</span>
              <span class="dg-sec-hint">沿用当前提示词，或连参考图一起改</span>
            </header>
            <RegenWorkbench
              v-model:mode="regenMode"
              :mode-options="regenModeOptions"
              :rows="regenRefRows"
              :slots-count="kfSlots.length"
              :bound-count="boundCount"
              :editable="regenRefsEditable"
              :validation="regenValidation"
              v-model:workflow-id="regenWorkflowId"
              v-model:params="regenParams"
              :schema="workflowSchema"
              @pick="openAssetPicker"
              @remove="removeRegenRef"
            />
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
/* 左图右文：左栏 sticky，滚动右栏时主操作常在视野内 */
.dg {
  display: grid;
  grid-template-columns: minmax(280px, 400px) minmax(0, 1fr);
  gap: var(--sp-6);
  align-items: start;
}
.dg-aside {
  min-width: 0;
}
.dg-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.dg-sec {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-4) 0;
  border-bottom: 1px solid var(--border-subtle);
}
.dg-sec:first-child {
  padding-top: 0;
}
.dg-sec.is-last {
  border-bottom: none;
  padding-bottom: 0;
}
.dg-sec-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.dg-sec-idx {
  flex: none;
  width: 18px;
  height: 18px;
  border-radius: var(--radius-sm);
  background: var(--brand-deep);
  color: var(--text-on-brand);
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}
.dg-sec-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.dg-sec-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-tertiary);
}
.dg-note {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
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
@media (max-width: 768px) {
  .dg-sec {
    padding: var(--sp-3) 0;
  }
  .dg-sec-name {
    font-size: 13px;
  }
  .dg-sec-hint {
    display: none;
  }
}
:deep(.n-card-header__main) {
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 600;
  letter-spacing: 0.01em;
}
</style>
