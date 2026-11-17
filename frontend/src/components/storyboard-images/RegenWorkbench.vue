<script setup lang="ts">
/**
 * 重新生成方式（纯展示）：模式 + 参考图槽位 + 预检结果。
 * 提示词已独立成 PromptPanel（详情右栏第 1 段），这里只管「传哪些图、怎么传」。
 * 状态与提交都在 useKeyframeRegen 里，本组件只渲染并抛出用户意图。
 */
import { NAlert, NButton, NImage, NRadioButton, NRadioGroup, NTag } from 'naive-ui'
import type { ValidateResult } from '@/api/regeneration'

interface WorkbenchRow {
  slot_param: string
  input_key?: string | null
  asset_id?: string | null
  asset_name?: string | null
  required?: boolean | null
  image_no: number | null
  label: string
}

defineProps<{
  mode: string
  modeOptions: Array<{ label: string; value: string }>
  rows: WorkbenchRow[]
  slotsCount: number
  boundCount: number
  editable: boolean
  validation: ValidateResult | null
}>()

const emit = defineEmits<{
  'update:mode': [string]
  pick: [string]
  remove: [string]
}>()
</script>

<template>
  <div class="wb">
    <div class="wb-head">
      <span class="wb-title">
        参考图槽位
        <b class="wb-count">{{ boundCount }}/{{ slotsCount }}</b>
      </span>
      <n-radio-group :value="mode" size="small" @update:value="(v: string) => emit('update:mode', v)">
        <n-radio-button v-for="opt in modeOptions" :key="opt.value" :value="opt.value">
          {{ opt.label }}
        </n-radio-button>
      </n-radio-group>
    </div>

    <div class="wb-list">
      <div v-for="row in rows" :key="row.slot_param" class="wb-item" :class="{ empty: !row.asset_id }">
        <span class="wb-no">Image {{ row.image_no ?? '—' }}</span>
        <n-image
          v-if="row.asset_id"
          :src="`/api/assets/${row.asset_id}/file`"
          :width="44"
          :height="44"
          object-fit="cover"
          class="wb-thumb"
          :img-props="{ style: 'width:44px;height:44px;object-fit:cover;border-radius:var(--radius-sm)' }"
        />
        <div v-else class="wb-thumb wb-thumb-empty">空槽位</div>
        <div class="wb-meta">
          <div class="wb-name">
            <span class="wb-name-text">{{ row.asset_name || row.label }}</span>
            <n-tag size="tiny" :bordered="false">{{ row.label }}</n-tag>
            <n-tag v-if="row.required" size="tiny" type="warning" :bordered="false">必需</n-tag>
          </div>
          <div class="wb-sub">{{ row.slot_param }} · 输入位置 {{ row.input_key ?? '—' }}</div>
        </div>
        <div v-if="editable" class="wb-actions">
          <n-button size="tiny" secondary @click="emit('pick', row.slot_param)">
            {{ row.asset_id ? '替换' : '新增' }}
          </n-button>
          <n-button
            v-if="row.asset_id"
            size="tiny"
            quaternary
            type="error"
            :disabled="!!row.required"
            @click="emit('remove', row.slot_param)"
          >
            移除
          </n-button>
        </div>
      </div>
    </div>

    <p v-if="!editable" class="wb-hint">
      当前为「沿用当前」：按提示词里的引用与槽位自动推导要传的图。要换图请切到「修改参考图」。
    </p>

    <template v-if="validation">
      <n-alert v-if="validation.errors.length" type="error" :bordered="false" title="校验未通过">
        <div v-for="e in validation.errors" :key="e.message">{{ e.message }}</div>
      </n-alert>
      <n-alert v-else-if="validation.warnings.length" type="warning" :bordered="false" title="校验有警告">
        <div v-for="w in validation.warnings" :key="w.message">{{ w.message }}</div>
      </n-alert>
      <n-alert v-else type="success" :bordered="false" title="校验通过，可以提交生成" />
    </template>
  </div>
</template>

<style scoped>
.wb {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.wb-head {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  flex-wrap: wrap;
}
.wb-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.wb-count {
  margin-left: 4px;
  font-family: var(--font-mono);
  font-weight: 400;
  color: var(--text-tertiary);
}
/* 槽位可能多达 9 个：宽栏两列排，避免一长串把右栏撑到要滚两屏 */
.wb-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
  gap: var(--sp-1) var(--sp-2);
}
.wb-item {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  transition: border-color var(--transition-fast);
}
.wb-item:hover {
  border-color: var(--border-strong);
}
.wb-item.empty {
  border-style: dashed;
  background: var(--bg-sunken);
}
.wb-no {
  flex: none;
  min-width: 58px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}
.wb-thumb {
  flex: none;
  background: var(--bg-sunken);
}
.wb-thumb-empty {
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-sm);
  font-size: 10px;
  color: var(--text-tertiary);
}
.wb-meta {
  flex: 1;
  min-width: 0;
}
.wb-name {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 13px;
}
.wb-name-text {
  max-width: 14em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wb-sub {
  margin-top: 2px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-tertiary);
}
.wb-actions {
  display: flex;
  gap: 6px;
}
.wb-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

@media (max-width: 768px) {
  .wb-item {
    flex-wrap: wrap;
  }
  .wb-actions {
    width: 100%;
    justify-content: flex-end;
  }
}
</style>
