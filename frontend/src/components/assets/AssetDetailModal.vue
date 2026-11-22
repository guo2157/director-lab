<script setup lang="ts">
/**
 * 资产详情：左图右文（窄屏回落单列）。
 * 只出现在点击卡片之后——列表页不再为单张图展开一大块详情。
 */
import { computed, ref, watch } from 'vue'
import { NButton, NImage, NModal } from 'naive-ui'
import StatusTag from '@/components/ui/StatusTag.vue'
import PromptEditor from '@/components/ui/PromptEditor.vue'
import { saveAssetRegeneratePrompt } from '@/api/assets'
import DependencyImpactPanel from '@/components/ui/DependencyImpactPanel.vue'
import { assetImgUrl } from './assetMedia'
import type { Asset } from '@/types/asset'

const props = withDefaults(
  defineProps<{
    show: boolean
    asset: Asset | null
    /** 生成提示词：优先是用户保存过的编辑值，否则为模板默认值 */
    prompt?: string
    promptVersion?: string
    /** 提示词来源：edited=用户保存值 / template=模板默认值 */
    promptSource?: 'edited' | 'template'
  }>(),
  { prompt: '', promptVersion: '', promptSource: 'template' },
)

const emit = defineEmits<{
  'update:show': [boolean]
  replace: []
  /** 重新生成：携带详情页当前（可能刚编辑过的）提示词 */
  regenerate: [prompt: string]
  copy: [string]
}>()

const TYPE_LABELS: Record<string, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
  keyframe: '分镜图',
  clip: '视频',
  audio: '音频',
  last_frame: '末帧',
  reference: '参考图',
  first_frame: '首帧',
  end_frame: '末帧',
}

const typeLabel = computed(() => TYPE_LABELS[props.asset?.asset_type ?? ''] ?? props.asset?.asset_type ?? '')
const imgUrl = computed(() => assetImgUrl(props.asset))
const canRegenerate = computed(() => ['character', 'scene', 'prop'].includes(props.asset?.asset_type ?? ''))
/** 只映射已知状态；未知/空状态不渲染标签，避免出现「未知状态」这种无信息量的描述 */
const STATUS_MAP: Record<string, { kind: 'succeeded' | 'stale' | 'pending'; label: string }> = {
  current: { kind: 'succeeded', label: '当前版本' },
  stale: { kind: 'stale', label: '已过期' },
  pending: { kind: 'pending', label: '待处理' },
  archived: { kind: 'stale', label: '已归档' },
}
const statusMeta = computed(() => STATUS_MAP[(props.asset?.status ?? '').toLowerCase()] ?? null)

/** 详情页提示词：可编辑。打开弹窗/切换资产时回显（优先用户保存值，其次模板默认值） */
const promptModel = ref('')
watch(
  () => [props.show, props.asset?.id, props.prompt] as const,
  () => {
    promptModel.value = props.prompt
  },
  { immediate: true },
)
/** 重新生成：先落盘当前提示词，再把这份提示词交给重新生成流程，避免被模板默认值覆盖 */
async function regenerateWithPrompt() {
  const asset = props.asset
  if (!asset || !canRegenerate.value) return
  try {
    await saveAssetRegeneratePrompt(asset.id, { prompt: promptModel.value })
  } catch {
    // 保存失败不阻断：本次重新生成仍会显式携带这份提示词
  }
  emit('regenerate', promptModel.value)
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="资产详情"
    closable
    :bordered="false"
    :style="{ width: 'min(1080px, 92vw)' }"
    :content-style="{ maxHeight: 'calc(85vh - 56px)', overflowY: 'auto' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div v-if="asset" class="detail-grid">
      <div class="detail-preview">
        <n-image v-if="asset.url" :src="imgUrl" object-fit="contain" class="detail-img" />
        <div v-else class="detail-empty">暂无预览</div>
      </div>

      <div class="detail-body">
        <div class="detail-title-row">
          <span class="detail-name">{{ asset.name || asset.id }}</span>
          <StatusTag v-if="statusMeta" :status="statusMeta.kind" :label="statusMeta.label" />
        </div>

        <div class="detail-meta">
          <div><span class="field-label">ID：</span>{{ asset.id }}</div>
          <div><span class="field-label">类型：</span>{{ typeLabel }}</div>
          <div v-if="typeof asset.reference_count === 'number'">
            <span class="field-label">引用次数：</span>{{ asset.reference_count }}
          </div>
        </div>

        <PromptEditor
          v-model="promptModel"
          :label="promptVersion ? `生成提示词 · v${promptVersion}` : '生成提示词'"
          :placeholder="canRegenerate ? '可在此编辑，重新生成会用这份提示词' : '暂无提示词'"
          :disabled="!canRegenerate"
          @copy="(v: string) => emit('copy', v)"
        />

        <div v-if="asset.project_id" class="detail-section">
          <div class="detail-section-head">
            <span class="detail-section-title">依赖影响</span>
            <span class="detail-section-hint">只读预演，不会自动重做</span>
          </div>
          <DependencyImpactPanel
            :project-id="asset.project_id"
            node-type="asset"
            :node-id="asset.id"
          />
        </div>

        <div class="detail-actions">
          <n-button size="small" @click="emit('replace')">上传替换</n-button>
          <n-button
            size="small"
            type="primary"
            :disabled="!canRegenerate"
            @click="regenerateWithPrompt"
          >
            重新生成
          </n-button>
        </div>
      </div>
    </div>
  </n-modal>
</template>

<style scoped>
.detail-grid {
  display: grid;
  grid-template-columns: minmax(240px, 340px) minmax(0, 1fr);
  gap: var(--sp-6);
  align-items: start;
}
.detail-preview {
  position: sticky;
  top: 0;
}
.detail-img {
  width: 100%;
  max-height: 460px;
  display: block;
}
.detail-empty {
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
.detail-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  min-width: 0;
}
.detail-title-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.detail-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}
.detail-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--sp-2) var(--sp-4);
  font-size: 13px;
  color: var(--text-secondary);
}
.field-label {
  color: var(--text-tertiary);
  font-size: 13px;
}
.detail-section {
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--sp-3);
}
.detail-section-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}
.detail-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
/* 分区标题行内的说明文字：与标题同一基线，不继承 prompt 提示的负边距 */
.detail-section-hint {
  font-size: 11px;
  color: var(--text-tertiary);
}
.detail-prompt-version {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-tertiary);
}
.detail-prompt {
  padding: var(--sp-3);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-prompt-hint {
  margin-top: calc(-1 * var(--sp-2));
  font-size: 11px;
  color: var(--text-tertiary);
}
.detail-prompt-hint .is-dirty {
  color: var(--state-warning);
}
.detail-actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
@media (max-width: 768px) {
  .detail-grid {
    grid-template-columns: 1fr;
    gap: var(--sp-4);
  }
  .detail-preview {
    position: static;
  }
}
</style>
