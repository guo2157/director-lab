<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation } from '@tanstack/vue-query'
import { NButton, NCard, NForm, NFormItem, NInput, NSpace, useMessage } from 'naive-ui'
import type { FormInst, FormRules } from 'naive-ui'
import { createSeries } from '@/api/series'
import type { SeriesCreatePayload } from '@/types/series'

const router = useRouter()
const message = useMessage()
const formRef = ref<FormInst | null>(null)

const form = ref({
  name: '',
  description: '',
  genre: '',
  style: '',
  target_audience: '',
  language: 'zh-CN',
  world: '',
  tone: '',
  main_characters: '',
})

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
    genre: form.value.genre.trim() || null,
    style: form.value.style.trim() || null,
    target_audience: form.value.target_audience.trim() || null,
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
    <n-card title="新建系列">
      <n-form ref="formRef" :model="form" :rules="rules" label-placement="top">
        <n-form-item label="系列名称" path="name">
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
        <n-form-item label="题材">
          <n-input v-model:value="form.genre" placeholder="例如：儿童成长 / 奇幻冒险" maxlength="64" />
        </n-form-item>
        <n-form-item label="整体风格">
          <n-input v-model:value="form.style" placeholder="例如：温暖治愈的手绘风格" maxlength="200" />
        </n-form-item>
        <n-form-item label="目标观众">
          <n-input v-model:value="form.target_audience" placeholder="例如：6-10 岁儿童" maxlength="64" />
        </n-form-item>

        <n-card size="small" title="初始系列设定（生成 Series Bible v1）" class="bible-card">
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
        </n-card>

        <n-space justify="end" class="actions">
          <n-button @click="goBack">取消</n-button>
          <n-button type="primary" :loading="mutation.isPending.value" @click="onSubmit">创建系列</n-button>
        </n-space>
      </n-form>
    </n-card>
  </div>
</template>

<style scoped>
.series-create-page {
  padding: 24px;
  max-width: 820px;
  margin: 0 auto;
}
.bible-card {
  margin: 8px 0 20px;
  background: #fafafa;
}
.hint {
  color: #888;
  font-size: 12px;
  margin: 0 0 12px;
  line-height: 1.6;
}
.actions {
  margin-top: 8px;
}
</style>
