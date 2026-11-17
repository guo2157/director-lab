<script setup lang="ts">
/**
 * 重新生成工作台（纯展示）：模式、参考图槽位、正向提示词、预检结果。
 * 状态与提交都在 useKeyframeRegen 里，这里只负责渲染与抛出用户意图。
 */
import { NAlert, NButton, NImage, NInput, NRadioButton, NRadioGroup, NTag } from 'naive-ui'
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

withDefaults(
  defineProps<{
    prompt: string
    mode: string
    modeOptions: Array<{ label: string; value: string }>
    rows: WorkbenchRow[]
    slotsCount: number
    boundCount: number
    editable: boolean
    validation: ValidateResult | null
  }>(),
  {},
)

const emit = defineEmits<{
  'update:prompt': [string]
  'update:mode': [string]
  pick: [string]
  remove: [string]
}>()
</script>

<template>
  <div class="wb">
    <div class="wb-head">
      <span class="wb-title">参考图槽位（{{ boundCount }}/{{ slotsCount }} 已绑定）</span>
      <n-radio-group :value="mode" size="small" @update:value="(v: string) => emit('update:mode', v)">
        <n-radio-button v-for="opt in modeOptions" :key="opt.value" :value="opt.value">
          {{ opt.label }}
        </n-radio-button>
      </n-radio-group>
    </div>

    <div class="wb-list">
      <div v-for="row in rows" :key="row.slot_param" class="wb-item" :class="{ empty: !row.asset_id }">
        <div class="wb-no">Image {{ row.image_no ?? '—' }}</div>
        <n-image
          v-if="row.asset_id"
          :src="`/api/assets/${row.asset_id}/file`"
          :width="52"
          :height="52"
          object-fit="cover"
          class="wb-thumb"
          :img-props="{ style: 'width:52px;height:52px;object-fit:cover;border-radius:var(--radius)' }"
        />
        <div v-else class="wb-thumb wb-thumb-empty">空槽位</div>
        <div class="wb-meta">
          <div class="wb-name">
            {{ row.asset_name || row.label }}
            <n-tag size="tiny" :bordered="false">{{ row.label }}</n-tag>
            <n-tag v-if="row.required" size="tiny" type="warning" :bordered="false">必需</n-tag>
          </div>
          <div class="wb-sub">槽位 {{ row.slot_param }} · 输入位置 {{ row.input_key ?? '—' }}</div>
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
    <div v-if="!editable" class="wb-hint">切换到『修改参考图』模式后可新增、替换或移除参考图。</div>

    <div class="wb-title">正向提示词</div>
    <n-input
      :value="prompt"
      type="textarea"
      :rows="6"
      placeholder="分镜图提示词（Image 编号与上方参考图槽位对应）"
      @update:value="(v: string) => emit('update:prompt', v)"
    />
    <div class="wb-hint">提示词中的 &lt;imageN&gt; 编号与上方参考图槽位严格对应；提交前会自动校验一致性。</div>

    <template v-if="validation">
      <div class="wb-title">校验结果</div>
      <n-alert v-if="validation.errors.length" type="error" :bordered="false">
        <div v-for="e in validation.errors" :key="e.message">✗ {{ e.message }}</div>
      </n-alert>
      <n-alert v-else-if="validation.warnings.length" type="warning" :bordered="false">
        <div v-for="w in validation.warnings" :key="w.message">⚠ {{ w.message }}</div>
      </n-alert>
      <n-alert v-else type="success" :bordered="false">✓ 校验通过，可以提交生成</n-alert>
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
  margin-bottom: var(--sp-1);
}
.wb-title {
  margin: var(--sp-2) 0 var(--sp-1);
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.wb-head .wb-title {
  margin: 0;
}
.wb-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.wb-item {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
}
.wb-item.empty {
  border-style: dashed;
  opacity: 0.75;
}
.wb-no {
  min-width: 64px;
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
}
.wb-thumb {
  flex: none;
  background: var(--bg-sunken);
}
.wb-thumb-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
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
.wb-sub {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-tertiary);
}
.wb-actions {
  display: flex;
  gap: 6px;
}
.wb-hint {
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
</style>
