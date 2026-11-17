<script setup lang="ts">
/**
 * 资产详情：左图右文（窄屏回落单列）。
 * 只出现在点击卡片之后——列表页不再为单张图展开一大块详情。
 */
import { computed } from 'vue'
import { NButton, NImage, NModal } from 'naive-ui'
import StatusTag from '@/components/ui/StatusTag.vue'
import PromptEditor from '@/components/ui/PromptEditor.vue'
import { assetImgUrl } from './assetMedia'
import type { Asset } from '@/types/asset'

const props = withDefaults(
  defineProps<{
    show: boolean
    asset: Asset | null
    /** 生成提示词（只读展示，读取失败时为空） */
    prompt?: string
    promptVersion?: string
  }>(),
  { prompt: '', promptVersion: '' },
)

const emit = defineEmits<{
  'update:show': [boolean]
  replace: []
  regenerate: []
  'toggle-lock': []
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
const statusKind = computed(() => {
  const s = (props.asset?.status ?? '').toLowerCase()
  if (s === 'current') return 'succeeded' as const
  if (s === 'stale') return 'stale' as const
  if (s === 'archived') return 'locked' as const
  return 'pending' as const
})
const statusLabel = computed(() => {
  const s = (props.asset?.status ?? '').toLowerCase()
  const map: Record<string, string> = { pending: '待处理', current: '当前版本', stale: '已过期', archived: '已归档' }
  return map[s] ?? (props.asset?.status || '未知状态')
})
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
          <StatusTag :status="statusKind" :label="statusLabel" />
          <span v-if="asset.locked" class="status-tag is-locked">已锁定</span>
        </div>

        <div class="detail-meta">
          <div><span class="field-label">ID：</span>{{ asset.id }}</div>
          <div><span class="field-label">类型：</span>{{ typeLabel }}</div>
          <div v-if="typeof asset.reference_count === 'number'">
            <span class="field-label">引用次数：</span>{{ asset.reference_count }}
          </div>
        </div>

        <PromptEditor
          v-if="prompt"
          :model-value="prompt"
          :label="promptVersion ? `生成提示词 · v${promptVersion}` : '生成提示词'"
          placeholder="暂无提示词"
          disabled
          @copy="(v: string) => emit('copy', v)"
        />

        <div class="detail-actions">
          <n-button size="small" :disabled="asset.locked" @click="emit('replace')">上传替换</n-button>
          <n-button
            size="small"
            type="primary"
            :disabled="asset.locked || !canRegenerate"
            @click="emit('regenerate')"
          >
            重新生成
          </n-button>
          <n-button size="small" quaternary @click="emit('toggle-lock')">
            {{ asset.locked ? '解锁' : '锁定' }}
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
.detail-actions {
  display: flex;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
@media (max-width: 900px) {
  .detail-grid {
    grid-template-columns: 1fr;
    gap: var(--sp-4);
  }
  .detail-preview {
    position: static;
  }
}
</style>
