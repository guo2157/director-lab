<script setup lang="ts">
import { computed } from "vue"
import { NSelect, NInput, NTag, NDivider } from "naive-ui"
import type { StyleConfig, StyleOptions } from "@/types/project"

/**
 * 纯展示型风格配置表单：只负责渲染与双向绑定，不做任何网络请求。
 * 供新建项目弹窗（项目 ID 尚不存在）与剧本页沿用同一套字段与交互。
 */
const props = defineProps<{ modelValue: StyleConfig; options: StyleOptions }>()
const emit = defineEmits<{ (e: "update:modelValue", v: StyleConfig): void }>()

const form = computed({
  get: () => props.modelValue,
  set: (v: StyleConfig) => emit("update:modelValue", v),
})

/** 下拉可选项统一转为 {label,value} 结构（展示名来自后端，前端不硬编码） */
function toSelectOptions(items?: Array<{ value: string; label: string; description?: string }>) {
  return (items ?? []).map((i) => ({ label: i.label, value: i.value }))
}

const genreGroups = computed(() =>
  (props.options.genre_groups ?? []).map((g) => ({
    type: "group" as const,
    label: g.group,
    key: g.group,
    children: g.tags.map((t) => ({ label: t, value: t })),
  })),
)
const toneOptions = computed(() =>
  (props.options.emotional_tones ?? []).map((t) => ({ label: t, value: t })),
)
const visualStyleHint = computed(() => {
  const key = form.value.visual_style
  return (props.options.visual_styles ?? []).find((v) => v.value === key)?.description ?? ""
})
const customLength = computed(() => form.value.custom_style_requirements?.length ?? 0)
const customMax = computed(() => props.options.max_custom_length ?? 500)

function patch(part: Partial<StyleConfig>) {
  emit("update:modelValue", { ...form.value, ...part })
}
</script>

<template>
  <div class="style-form">
    <div class="block">
      <div class="block-title">剧本类型</div>
      <div class="block-hint">多选。第一个为主类型，其余为辅助标签。留空则使用默认配置。</div>
      <NSelect
        :value="form.genre_tags"
        multiple
        filterable
        clearable
        :options="genreGroups"
        placeholder="选择题材标签（按分组）"
        max-tag-count="responsive"
        @update:value="(v: string[]) => patch({ genre_tags: v })"
      />
    </div>

    <div class="block">
      <div class="block-title">叙事风格</div>
      <div class="block-hint">多选。第一个为主风格，不会把叙事标签直接当成视觉描述。</div>
      <NSelect
        :value="form.narrative_styles"
        multiple
        filterable
        clearable
        :options="toSelectOptions(options.narrative_styles)"
        placeholder="选择叙事风格"
        max-tag-count="responsive"
        @update:value="(v: string[]) => patch({ narrative_styles: v })"
      />
      <div v-if="form.narrative_styles?.length" class="chip-list">
        <NTag v-for="name in form.narrative_styles" :key="name" size="small" :bordered="false" type="info">
          <span class="chip-label">{{ name }}</span>
          <span class="chip-desc">{{ options.narrative_styles.find((o) => o.value === name)?.description }}</span>
        </NTag>
      </div>
    </div>

    <div class="block">
      <div class="block-title">主视觉风格<em class="required">必填 · 单选</em></div>
      <div class="block-hint">一个项目只能有一个主视觉风格，资产图与分镜共用同一美术方向。</div>
      <div class="style-grid">
        <button
          v-for="item in options.visual_styles"
          :key="item.value"
          type="button"
          class="style-card"
          :class="{ active: form.visual_style === item.value }"
          @click="patch({ visual_style: item.value })"
        >
          <span class="card-label">{{ item.label }}</span>
          <span class="card-desc">{{ item.description }}</span>
        </button>
      </div>
      <div v-if="visualStyleHint" class="block-hint">当前风格：{{ visualStyleHint }}</div>
    </div>

    <NDivider title-placement="left">节奏与情绪</NDivider>
    <div class="row">
      <div class="field">
        <div class="field-label">剧情节奏</div>
        <NSelect
          :value="form.pacing"
          :options="toSelectOptions(options.pacing)"
          @update:value="(v: string) => patch({ pacing: v })"
        />
      </div>
      <div class="field">
        <div class="field-label">冲突强度</div>
        <NSelect
          :value="form.conflict_intensity"
          :options="toSelectOptions(options.conflict_intensity)"
          @update:value="(v: string) => patch({ conflict_intensity: v })"
        />
      </div>
      <div class="field">
        <div class="field-label">反转密度</div>
        <NSelect
          :value="form.twist_density"
          :options="toSelectOptions(options.twist_density)"
          @update:value="(v: string) => patch({ twist_density: v })"
        />
      </div>
    </div>

    <div class="block">
      <div class="field-label">情绪基调</div>
      <div class="block-hint">可多选，第一个为主情绪；单镜头不会同时体现全部情绪。</div>
      <NSelect
        :value="form.emotional_tone"
        multiple
        clearable
        :options="toneOptions"
        placeholder="选择情绪基调"
        @update:value="(v: string[]) => patch({ emotional_tone: v })"
      />
    </div>

    <NDivider title-placement="left">表达设置</NDivider>
    <div class="row">
      <div class="field">
        <div class="field-label">对白风格</div>
        <NSelect
          :value="form.dialogue_style"
          :options="toSelectOptions(options.dialogue_styles)"
          @update:value="(v: string) => patch({ dialogue_style: v })"
        />
      </div>
      <div class="field">
        <div class="field-label">叙述方式</div>
        <NSelect
          :value="form.narration_mode"
          :options="toSelectOptions(options.narration_modes)"
          @update:value="(v: string) => patch({ narration_mode: v })"
        />
      </div>
      <div class="field">
        <div class="field-label">目标受众</div>
        <NSelect
          :value="form.target_audience"
          :options="toSelectOptions(options.target_audiences)"
          @update:value="(v: string) => patch({ target_audience: v })"
        />
      </div>
    </div>

    <div class="block">
      <div class="block-title">自定义要求<em class="optional">可选</em></div>
      <div class="block-hint">只补充风格与表达要求，不覆盖资产绑定、审核状态与工作流必需参数。</div>
      <NInput
        :value="form.custom_style_requirements"
        type="textarea"
        :maxlength="customMax"
        show-count
        :autosize="{ minRows: 2, maxRows: 5 }"
        placeholder="例如：角色造型适合儿童观看，画面清楚，避免恐怖和过度刺激的内容。"
        @update:value="(v: string) => patch({ custom_style_requirements: v })"
      />
      <div class="counter">{{ customLength }} / {{ customMax }}</div>
    </div>
  </div>
</template>

<style scoped>
.style-form { display: flex; flex-direction: column; gap: 14px; }
.block { display: flex; flex-direction: column; gap: 6px; }
.block-title { font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.block-title em { font-style: normal; font-size: 11px; font-weight: 500; padding: 0 6px; border-radius: 8px; }
.required { color: #fff; background: var(--brand-primary, #6366f1); }
.optional { color: var(--text-secondary); background: var(--bg-elevated, #f1f5f9); }
.block-hint { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }
.row { display: flex; gap: 12px; flex-wrap: wrap; }
.field { flex: 1 1 180px; display: flex; flex-direction: column; gap: 6px; min-width: 160px; }
.field-label { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.chip-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.chip-label { font-weight: 600; margin-right: 6px; }
.chip-desc { font-size: 11px; color: var(--text-secondary); }
.style-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.style-card {
  text-align: left; cursor: pointer; border-radius: 8px; padding: 8px 10px;
  border: 1px solid var(--border-subtle, #e5e7eb); background: var(--bg-elevated, #f8fafc);
  display: flex; flex-direction: column; gap: 4px; transition: border-color 0.15s, box-shadow 0.15s;
}
.style-card:hover { border-color: var(--brand-primary, #6366f1); }
.style-card.active { border-color: var(--brand-primary, #6366f1); box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.15); }
.card-label { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.card-desc { font-size: 11px; color: var(--text-secondary); line-height: 1.5; }
.counter { font-size: 11px; color: var(--text-tertiary); text-align: right; }
</style>
