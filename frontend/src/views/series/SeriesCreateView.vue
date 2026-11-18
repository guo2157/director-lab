<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useQuery, useMutation } from '@tanstack/vue-query'
import { NButton, NCard, NForm, NFormItem, NInput, NSelect, useMessage } from 'naive-ui'
import type { FormInst, FormRules, SelectOption } from 'naive-ui'
import { createSeries } from '@/api/series'
import { getStyleOptions } from '@/api/projects'
import type { SeriesCreatePayload } from '@/types/series'

const router = useRouter()
const message = useMessage()
const formRef = ref<FormInst | null>(null)

const form = ref({
  name: '',
  description: '',
  genre: null as string | null,
  style: null as string | null,
  target_audience: null as string | null,
  language: 'zh-CN',
  world: '',
  tone: '',
  main_characters: '',
})

/** 与单集项目共用后端风格选项，避免人工填写口径不一致 */
const styleOptionsQuery = useQuery({
  queryKey: ['style-options'],
  queryFn: () => getStyleOptions(),
  staleTime: 5 * 60 * 1000,
})
const styleOptions = computed(() => styleOptionsQuery.data.value)
const genreOptions = computed<SelectOption[]>(() =>
  (styleOptions.value?.genre_groups ?? []).map((g) => ({
    type: 'group' as const,
    label: g.group,
    key: g.group,
    children: g.tags.map((t) => ({ label: t, value: t })),
  })),
)
const visualStyleOptions = computed<SelectOption[]>(() =>
  (styleOptions.value?.visual_styles ?? []).map((s) => ({ label: s.label, value: s.label })),
)
const audienceOptions = computed<SelectOption[]>(() =>
  (styleOptions.value?.target_audiences ?? []).map((a) => ({ label: a.label, value: a.label })),
)
const styleHint = computed(
  () => (styleOptions.value?.visual_styles ?? []).find((s) => s.label === form.value.style)?.description ?? '',
)

const rules: FormRules = {
  name: [
    { required: true, message: '请输入系列名称', trigger: ['input', 'blur'] },
    { max: 200, message: '名称不能超过 200 个字符', trigger: ['input', 'blur'] },
  ],
  description: [{ max: 2000, message: '简介不能超过 2000 个字符', trigger: ['input', 'blur'] }],
}

const mutation = useMutation({
  mutationFn: (payload: SeriesCreatePayload) => createSeries(payload),
  onSuccess: (data) => {
    message.success('系列已创建，并生成初始设定版本 v1')
    void router.push({ name: 'series-detail', params: { id: data.id } })
  },
  onError: (err: any) => message.error(err?.message ?? '创建失败'),
})

async function onSubmit() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  const bible: Record<string, unknown> = {}
  if (form.value.world.trim()) bible.world = form.value.world.trim()
  if (form.value.tone.trim()) bible.tone = form.value.tone.trim()
  if (form.value.main_characters.trim()) bible.main_characters = form.value.main_characters.trim()

  mutation.mutate({
    name: form.value.name.trim(),
    description: form.value.description.trim() || null,
    genre: form.value.genre?.trim() || null,
    style: form.value.style?.trim() || null,
    target_audience: form.value.target_audience?.trim() || null,
    language: form.value.language || 'zh-CN',
    bible: Object.keys(bible).length > 0 ? bible : null,
  })
}

function goBack() {
  void router.push({ name: 'series-list' })
}
</script>

<template>
  <div class="series-create-page">
    <header class="page-head">
      <div class="head-text">
        <h1 class="page-title">新建系列</h1>
        <p class="page-sub">统一世界观与角色资产，后续可按集续写。带 <em>*</em> 的为必填项。</p>
      </div>
    </header>

    <n-card :bordered="false" class="form-card">
      <n-form ref="formRef" :model="form" :rules="rules" label-placement="top">
        <section class="form-section">
          <div class="section-title">基础信息</div>
          <n-form-item label="系列名称" path="name" required>
            <n-input v-model:value="form.name" placeholder="例如：小熊成长记" maxlength="200" show-count />
          </n-form-item>
          <n-form-item label="系列简介" path="description">
            <n-input
              v-model:value="form.description"
              type="textarea"
              placeholder="一句话说明这个系列讲什么"
              :autosize="{ minRows: 2, maxRows: 4 }"
              maxlength="2000"
              show-count
            />
          </n-form-item>
        </section>

        <section class="form-section">
          <div class="section-title">题材与风格</div>
          <div class="form-row">
            <n-form-item label="题材类型" class="form-col">
              <n-select
                v-model:value="form.genre"
                :options="genreOptions"
                placeholder="选择题材类型"
                filterable
                clearable
              />
            </n-form-item>
            <n-form-item label="目标受众" class="form-col">
              <n-select
                v-model:value="form.target_audience"
                :options="audienceOptions"
                placeholder="选择目标受众"
                filterable
                clearable
              />
            </n-form-item>
          </div>
          <n-form-item label="整体风格">
            <div class="style-field">
              <n-select
                v-model:value="form.style"
                :options="visualStyleOptions"
                placeholder="选择整体视觉风格"
                filterable
                clearable
              />
              <div v-if="styleHint" class="field-hint">{{ styleHint }}</div>
            </div>
          </n-form-item>
        </section>

        <section class="form-section">
          <div class="section-title">
            初始系列设定
            <span class="section-badge">生成 Series Bible v1</span>
          </div>
          <p class="hint">这些内容会作为已确认设定进入后续每次续写的上下文。后续修改会生成新版本，不覆盖历史版本。</p>
          <n-form-item label="世界观与背景">
            <n-input
              v-model:value="form.world"
              type="textarea"
              placeholder="故事发生的世界、规则与背景"
              :autosize="{ minRows: 2, maxRows: 5 }"
            />
          </n-form-item>
          <n-form-item label="基调与叙事风格">
            <n-input
              v-model:value="form.tone"
              type="textarea"
              placeholder="温情 / 冒险 / 幽默等基调要求"
              :autosize="{ minRows: 2, maxRows: 4 }"
            />
          </n-form-item>
          <n-form-item label="主要角色">
            <n-input
              v-model:value="form.main_characters"
              type="textarea"
              placeholder="列出主要角色及其固定身份特征，避免后续续写改变身份"
              :autosize="{ minRows: 2, maxRows: 5 }"
            />
          </n-form-item>
        </section>

        <div class="actions">
          <n-button @click="goBack">取消</n-button>
          <n-button type="primary" :loading="mutation.isPending.value" @click="onSubmit">创建系列</n-button>
        </div>
      </n-form>
    </n-card>
  </div>
</template>

<style scoped>
.series-create-page {
  padding: 24px;
  max-width: 880px;
  margin: 0 auto;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  color: var(--text-primary);
}
.page-sub {
  margin: 6px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.page-sub em {
  font-style: normal;
  color: var(--danger);
}
.form-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
}
.form-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 16px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border-subtle);
}
.form-section:last-of-type {
  border-bottom: none;
  margin-bottom: 8px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.section-badge {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  color: var(--brand-primary);
  background: var(--brand-glow);
}
.form-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.form-col {
  flex: 1 1 240px;
  min-width: 0;
}
.style-field {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.field-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.hint {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-tertiary);
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}
</style>
