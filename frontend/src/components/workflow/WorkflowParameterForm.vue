<script setup lang="ts">
/**
 * 动态参数表单（P5）：完全由后端 Schema 驱动，新增工作流不需要改这里。
 * - 只渲染 `parameters`（标量/文本）；图片/音频/视频槽位由 WorkflowReferenceSlots 负责
 * - 校验在前端做即时提示，后端仍会再校验一次（不依赖前端）
 * - 高级参数默认折叠（steps/cfg/seed 这类不常动的）
 */
import { computed, watch } from 'vue'
import {
  NCollapse, NCollapseItem, NFormItem, NInput, NInputNumber, NSelect, NSwitch,
} from 'naive-ui'
import type { WorkflowParamSchema, WorkflowSchema } from '@/types/workflow'

const props = withDefaults(defineProps<{
  schema: WorkflowSchema | null
  modelValue: Record<string, unknown>
  disabled?: boolean
}>(), { disabled: false })

const emit = defineEmits<{
  (e: 'update:modelValue', value: Record<string, unknown>): void
}>()

const basicParams = computed(() => (props.schema?.parameters ?? [])
  .filter((p) => !p.advanced && p.group !== 'advanced'))

const advancedParams = computed(() => (props.schema?.parameters ?? [])
  .filter((p) => p.advanced || p.group === 'advanced'))

/** 切换工作流时用 Schema 默认值补齐缺失键；已填的值不覆盖（不打断用户输入） */
watch(() => props.schema?.workflow_id, () => {
  if (!props.schema) return
  const next: Record<string, unknown> = { ...props.modelValue }
  let changed = false
  for (const p of props.schema.parameters) {
    if (next[p.key] === undefined && p.default !== null && p.default !== undefined) {
      next[p.key] = p.default
      changed = true
    }
  }
  if (changed) emit('update:modelValue', next)
})

function valueOf(key: string): unknown {
  const v = props.modelValue[key]
  return v === undefined ? null : v
}

function setValue(key: string, value: unknown) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function numberValue(key: string): number | null {
  const v = valueOf(key)
  return typeof v === 'number' ? v : null
}

function stringValue(key: string): string {
  const v = valueOf(key)
  return typeof v === 'string' ? v : ''
}

function boolValue(key: string): boolean {
  return Boolean(valueOf(key))
}

/** 前端即时校验：只覆盖 Schema 能表达的约束，其余交给后端 */
function validation(p: WorkflowParamSchema): { status?: 'error' | 'warning', feedback?: string } {
  const v = valueOf(p.key)
  if (p.required && (v === null || v === '' || v === undefined)) {
    return { status: 'error', feedback: '必填' }
  }
  if (typeof v === 'number') {
    if (p.min !== undefined && v < p.min) return { status: 'error', feedback: `不能小于 ${p.min}` }
    if (p.max !== undefined && v > p.max) return { status: 'error', feedback: `不能大于 ${p.max}` }
    if (p.step && p.step > 1 && (v - (p.min ?? 0)) % p.step !== 0) {
      return { status: 'warning', feedback: `应为 ${p.step} 的步进` }
    }
  }
  return {}
}

function isNumeric(p: WorkflowParamSchema): boolean {
  return p.type === 'integer' || p.type === 'number'
}
</script>

<template>
  <div v-if="schema" class="wf-params">
    <div v-if="schema.warnings.length" class="wf-warn">
      <span v-for="(w, i) in schema.warnings" :key="i">· {{ w }}</span>
    </div>

    <NFormItem
      v-for="p in basicParams"
      :key="p.key"
      :label="p.label || p.key"
      :validation-status="validation(p).status"
      :feedback="validation(p).feedback"
      :required="p.required"
      :show-feedback="Boolean(validation(p).feedback)"
    >
      <div class="wf-field">
        <NInput
          v-if="p.type === 'textarea'"
          :value="stringValue(p.key)"
          type="textarea"
          :autosize="{ minRows: 3, maxRows: 8 }"
          :disabled="disabled"
          :placeholder="p.placeholder || p.description || ''"
          @update:value="(v: string) => setValue(p.key, v)"
        />
        <NInput
          v-else-if="p.type === 'string'"
          :value="stringValue(p.key)"
          :disabled="disabled"
          :placeholder="p.placeholder || p.description || ''"
          @update:value="(v: string) => setValue(p.key, v)"
        />
        <NInputNumber
          v-else-if="isNumeric(p)"
          :value="numberValue(p.key)"
          :min="p.min"
          :max="p.max"
          :step="p.step"
          :disabled="disabled"
          :show-button="false"
          class="wf-number"
          @update:value="(v: number | null) => setValue(p.key, v)"
        />
        <NSelect
          v-else-if="p.type === 'select' || p.type === 'enum'"
          :value="(valueOf(p.key) as string | null)"
          :options="(p.options ?? []).map((o) => ({ label: String(o), value: String(o) }))"
          :disabled="disabled"
          clearable
          @update:value="(v: string | null) => setValue(p.key, v)"
        />
        <NSwitch
          v-else-if="p.type === 'boolean'"
          :value="boolValue(p.key)"
          :disabled="disabled"
          @update:value="(v: boolean) => setValue(p.key, v)"
        />
        <NInput
          v-else
          :value="stringValue(p.key)"
          :disabled="disabled"
          @update:value="(v: string) => setValue(p.key, v)"
        />
        <span v-if="p.description && p.type !== 'textarea'" class="wf-hint">{{ p.description }}</span>
      </div>
    </NFormItem>

    <NCollapse v-if="advancedParams.length" arrow-placement="right">
      <NCollapseItem title="高级参数" name="advanced">
        <NFormItem
          v-for="p in advancedParams"
          :key="p.key"
          :label="p.label || p.key"
          :validation-status="validation(p).status"
          :feedback="validation(p).feedback"
          :show-feedback="Boolean(validation(p).feedback)"
        >
          <NInputNumber
            v-if="isNumeric(p)"
            :value="numberValue(p.key)"
            :min="p.min"
            :max="p.max"
            :step="p.step"
            :disabled="disabled"
            :show-button="false"
            class="wf-number"
            @update:value="(v: number | null) => setValue(p.key, v)"
          />
          <NInput
            v-else
            :value="stringValue(p.key)"
            :disabled="disabled"
            @update:value="(v: string) => setValue(p.key, v)"
          />
        </NFormItem>
      </NCollapseItem>
    </NCollapse>
  </div>
</template>

<style scoped>
.wf-params {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.wf-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
}

.wf-number {
  width: 180px;
}

.wf-hint {
  font-size: 12px;
  color: var(--text-secondary);
}

.wf-warn {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
