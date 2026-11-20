<script setup lang="ts">
/**
 * 详情左栏：大图 + 元信息 + 常驻操作条。
 *
 * 「重新生成 / 替换上传」是这里唯一的主动作，平铺在图上，不进下拉、不折叠；
 * 校验放在次要位置（提交前自动校验，手动校验只是排错用）。
 */
import { computed } from 'vue'
import { NButton, NImage } from 'naive-ui'
import StatusTag from '@/components/ui/StatusTag.vue'
import type { StatusKind } from './types'

const props = withDefaults(
  defineProps<{
    url: string
    title: string
    seq: number
    statusKind: StatusKind
    statusLabel: string
    hasKeyframe?: boolean
    generating?: boolean
    validating?: boolean
  }>(),
  {
    hasKeyframe: false,
    generating: false,
    validating: false,
  },
)

const emit = defineEmits<{
  generate: []
  replace: []
  validate: []
}>()

const generateLabel = computed(() => {
  if (props.generating) return '提交中…'
  return props.hasKeyframe ? '重新生成分镜图' : '生成分镜图'
})
const generateTitle = computed(() => {
  return props.hasKeyframe ? '用当前提示词与参考图重绘本镜' : '按当前提示词与参考图生成'
})
const replaceTitle = computed(() => {
  if (!props.hasKeyframe) return '尚无分镜图，生成后才能替换'
  return '用本地上传的图替换该镜分镜图'
})
</script>

<template>
  <div class="sp">
    <div class="sp-frame">
      <n-image
        v-if="url"
        :src="url"
        :preview-src="url"
        class="sp-img"
        object-fit="contain"
        alt="分镜图"
      />
      <div v-else class="sp-empty">该镜头尚未生成分镜图</div>
    </div>

    <div class="sp-meta">
      <span class="sp-seq">镜 {{ seq }}</span>
      <span class="sp-name" :title="title">{{ title }}</span>
      <span class="sp-tags">
        <StatusTag :status="statusKind" :label="statusLabel" />
      </span>
    </div>

    <div class="sp-actions">
      <n-button
        type="primary"
        block
        :loading="generating"
        :disabled="generating"
        :title="generateTitle"
        @click="emit('generate')"
      >
        {{ generateLabel }}
      </n-button>
      <div class="sp-row">
        <n-button
          secondary
          :disabled="!hasKeyframe || generating"
          :title="replaceTitle"
          @click="emit('replace')"
        >
          替换上传
        </n-button>
        <n-button
          quaternary
          :loading="validating"
          :disabled="!hasKeyframe || generating"
          title="校验提示词中的图片引用与参考图槽位是否一致"
          @click="emit('validate')"
        >
          校验
        </n-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sp {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
}
.sp-frame {
  padding: var(--sp-2);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
}
.sp-img {
  display: block;
  width: 100%;
  border-radius: var(--radius);
  overflow: hidden;
}
.sp-img :deep(img) {
  width: 100%;
  max-height: 460px;
  object-fit: contain;
  display: block;
}
.sp-empty {
  padding: var(--sp-10) var(--sp-4);
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius);
  text-align: center;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.sp-meta {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  min-width: 0;
}
.sp-seq {
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 18px;
  padding: 0 var(--sp-2);
  color: var(--text-tertiary);
  background: var(--neutral-soft);
  border: 1px solid var(--neutral-line);
  border-radius: var(--radius-sm);
}
.sp-name {
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sp-tags {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  margin-left: auto;
  flex-wrap: wrap;
}
.sp-chip {
  padding: 0 var(--sp-2);
  font-size: 11px;
  line-height: 18px;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
}
.sp-chip.is-warn {
  color: var(--accent-amber);
  background: var(--warning-soft);
  border-color: var(--warning-line);
}
.sp-chip.is-ok {
  color: var(--state-success);
  background: var(--success-soft);
  border-color: var(--success-line);
}

.sp-actions {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding-top: var(--sp-1);
}
.sp-row {
  display: flex;
  gap: var(--sp-2);
}
.sp-row :deep(.n-button) {
  flex: 1;
}
</style>
