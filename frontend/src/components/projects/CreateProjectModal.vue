<script setup lang="ts">
/**
 * 新建作品弹窗：单集项目 / 系列两种创建类型。
 * 自持表单状态与创建 mutation，对外只暴露 open() 与 created 事件，避免父级堆状态。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMutation, useQuery } from '@tanstack/vue-query'
import {
  NButton,
  NForm,
  NFormItem,
  NInput,
  NModal,
  NSelect,
  useMessage,
} from 'naive-ui'
import type { FormInst, FormRules } from 'naive-ui'
import { createProject, getStyleOptions } from '@/api/projects'
import { createSeries } from '@/api/series'
import type { SeriesCreatePayload, SeriesListItem } from '@/types/series'
import type { AspectRatio, CreationType, CreateProjectPayload, Project, StyleConfig } from '@/types/project'
import { NIcon } from 'naive-ui'
import { FilmOutline, LibraryOutline } from '@vicons/ionicons5'
import StyleConfigForm from '@/components/StyleConfigForm.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const emit = defineEmits<{
  created: [project: Project]
  'created-series': [series: SeriesListItem]
}>()

const router = useRouter()
const confirmDialog = useConfirmDialog()
const message = useMessage()

const ASPECT_OPTIONS: AspectRatio[] = ['9:16', '16:9', '1:1']
const CREATION_OPTIONS: { value: CreationType; label: string; desc: string }[] = [
  { value: 'novel', label: '小说改编', desc: '从小说文本生成剧本' },
  { value: 'script', label: '剧本直出', desc: '直接编写剧本内容' },
]

const show = ref(false)
const createType = ref<'project' | 'series'>('project')
const formRef = ref<FormInst | null>(null)
const seriesFormRef = ref<FormInst | null>(null)
const formModel = ref<{ name: string; aspect_ratio: AspectRatio; creation_type: CreationType }>({
  name: '',
  aspect_ratio: '16:9',
  creation_type: 'novel',
})
const seriesForm = ref<{
  name: string
  description: string
  genre: string | null
  style: string | null
  target_audience: string | null
}>({
  name: '',
  description: '',
  genre: '',
  style: '',
  target_audience: '',
})

/* ---------- 可选风格配置（留空则完全使用后端默认值） ---------- */
const styleOptionsQuery = useQuery({
  queryKey: ['style-options'],
  queryFn: () => getStyleOptions(),
  staleTime: 5 * 60 * 1000,
})
const styleOptions = computed(() => styleOptionsQuery.data.value)
/** 系列表单下拉选项：与单集项目的风格配置共用后端选项，避免人工填写口径不一致 */
const seriesGenreOptions = computed(() =>
  (styleOptions.value?.genre_groups ?? []).map((g) => ({
    type: 'group' as const,
    label: g.group,
    key: g.group,
    children: g.tags.map((t) => ({ label: t, value: t })),
  })),
)
const seriesStyleOptions = computed(() =>
  (styleOptions.value?.visual_styles ?? []).map((s) => ({ label: s.label, value: s.label })),
)
const seriesAudienceOptions = computed(() =>
  (styleOptions.value?.target_audiences ?? []).map((a) => ({ label: a.label, value: a.label })),
)
const seriesStyleHint = computed(() => {
  const key = seriesForm.value.style
  return (styleOptions.value?.visual_styles ?? []).find((s) => s.label === key)?.description ?? ''
})
const showStyleConfig = ref(false)
const styleConfig = ref<StyleConfig | null>(null)
function toggleStyleConfig(v: boolean) {
  showStyleConfig.value = v
  if (v && !styleConfig.value && styleOptions.value) {
    styleConfig.value = { ...styleOptions.value.defaults }
  }
}

const rules: FormRules = {
  name: [
    { required: true, message: '请输入作品名称', trigger: ['blur', 'input'] },
    {
      validator: (_rule, value: string) => value.trim().length >= 1 && value.trim().length <= 100,
      message: '作品名称需为 1-100 个字符',
      trigger: ['blur', 'input'],
    },
  ],
}
const seriesRules: FormRules = {
  name: [
    { required: true, message: '请输入系列名称', trigger: ['blur', 'input'] },
    {
      validator: (_rule, value: string) => value.trim().length >= 1 && value.trim().length <= 200,
      message: '系列名称需为 1-200 个字符',
      trigger: ['blur', 'input'],
    },
  ],
  description: [{ max: 2000, message: '简介不能超过 2000 个字符', trigger: ['input', 'blur'] }],
}

function open() {
  createType.value = 'project'
  formModel.value = { name: '', aspect_ratio: '16:9', creation_type: 'novel' }
  seriesForm.value = { name: '', description: '', genre: '', style: '', target_audience: '' }
  showStyleConfig.value = false
  styleConfig.value = styleOptions.value ? { ...styleOptions.value.defaults } : null
  show.value = true
}
defineExpose({ open })

function onUpdateShow(v: boolean) {
  if (v) {
    show.value = true
    return
  }
  if (formModel.value.name.trim() !== '') {
    confirmDialog({
      title: '放弃未保存的修改？',
      message: '当前表单已填写内容，关闭后将丢失。',
      confirmText: '放弃修改',
      cancelText: '继续编辑',
      onConfirm: () => {
        show.value = false
      },
    })
  } else {
    show.value = false
  }
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败，请稍后重试'
}

const { mutate: mutateCreate, isPending: creating } = useMutation({
  mutationFn: createProject,
  onSuccess: (p) => {
    message.success(`作品「${p.name}」创建成功`)
    show.value = false
    emit('created', p)
  },
  onError: (e) => {
    // 配置未完成：阻断并引导用户前往设置页
    const code = (e as { code?: string })?.code
    if (code === 'llm_not_configured' || code === 'comfyui_not_configured') {
      const isLlm = code === 'llm_not_configured'
      confirmDialog({
        title: isLlm ? '语言模型未配置' : 'ComfyUI 未配置',
        message: isLlm
          ? '创建作品前需要先完成语言模型配置（API URL / API Key / 模型名称）。是否前往设置页面进行配置？'
          : '创建作品前需要先完成 ComfyUI 配置（至少一个启用实例）。是否前往设置页面进行配置？',
        confirmText: '前往设置',
        cancelText: '暂不创建',
        danger: false,
        onConfirm: () => {
          show.value = false
          void router.push('/settings')
        },
      })
      return
    }
    message.error(errMsg(e))
  },
})
const { mutate: mutateCreateSeries, isPending: creatingSeries } = useMutation({
  mutationFn: (payload: SeriesCreatePayload) => createSeries(payload),
  onSuccess: (s) => {
    message.success(`系列「${s.name}」创建成功`)
    show.value = false
    emit('created-series', s)
  },
  onError: (e) => {
    message.error(errMsg(e))
  },
})

async function handleCreate() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  const payload: CreateProjectPayload = {
    name: formModel.value.name.trim(),
    aspect_ratio: formModel.value.aspect_ratio,
    creation_type: formModel.value.creation_type,
  }
  // 仅当用户主动展开并修改过风格配置时才提交，避免覆盖后端默认基线
  if (showStyleConfig.value && styleConfig.value) {
    payload.style_config = { ...styleConfig.value }
  }
  mutateCreate(payload)
}
async function handleCreateSeries() {
  try {
    await seriesFormRef.value?.validate()
  } catch {
    return
  }
  const payload: SeriesCreatePayload = {
    name: seriesForm.value.name.trim(),
    description: (seriesForm.value.description ?? '').trim() || null,
    genre: (seriesForm.value.genre ?? '').trim() || null,
    style: (seriesForm.value.style ?? '').trim() || null,
    target_audience: (seriesForm.value.target_audience ?? '').trim() || null,
  }
  mutateCreateSeries(payload)
}
</script>

<template>
  <n-modal
    :show="show"
    :on-update:show="onUpdateShow"
    preset="card"
    title="新建作品"
    class="create-modal"
    :style="{ width: '520px' }"
    :mask-closable="false"
  >
    <!-- ===== 创建类型选择 ===== -->
    <div class="create-type-cards">
      <button
        type="button"
        class="create-type-card"
        :class="{ active: createType === 'project' }"
        :aria-pressed="createType === 'project'"
        @click="createType = 'project'"
      >
        <span class="ct-icon" aria-hidden="true"><n-icon :component="FilmOutline" :size="22" /></span>
        <span class="ct-name">单集项目</span>
        <span class="ct-desc">独立的单集漫剧，快速从剧本到成片</span>
      </button>
      <button
        type="button"
        class="create-type-card"
        :class="{ active: createType === 'series' }"
        :aria-pressed="createType === 'series'"
        @click="createType = 'series'"
      >
        <span class="ct-icon" aria-hidden="true"><n-icon :component="LibraryOutline" :size="22" /></span>
        <span class="ct-name">系列</span>
        <span class="ct-desc">多集长篇创作，统一世界观与角色资产</span>
      </button>
    </div>

    <!-- ===== 单集项目表单 ===== -->
    <n-form
      v-if="createType === 'project'"
      ref="formRef"
      :model="formModel"
      :rules="rules"
      label-placement="top"
      :show-require-mark="false"
    >
      <n-form-item label="作品名称" path="name">
        <n-input
          v-model:value="formModel.name"
          placeholder="请输入作品名称（1-100 字符）"
          maxlength="100"
        />
      </n-form-item>
      <n-form-item label="画幅比" path="aspect_ratio">
        <div class="ratio-cards">
          <button
            v-for="r in ASPECT_OPTIONS"
            :key="r"
            type="button"
            class="ratio-card"
            :class="{ active: formModel.aspect_ratio === r }"
            :aria-pressed="formModel.aspect_ratio === r"
            @click="formModel.aspect_ratio = r"
          >
            <span class="ratio-shape" :class="`shape-${r.replace(':', '-')}`" aria-hidden="true"></span>
            <span class="ratio-label">{{ r }}</span>
          </button>
        </div>
      </n-form-item>
      <n-form-item label="创作类型" path="creation_type">
        <div class="type-cards">
          <button
            v-for="t in CREATION_OPTIONS"
            :key="t.value"
            type="button"
            class="type-card"
            :class="{ active: formModel.creation_type === t.value }"
            :aria-pressed="formModel.creation_type === t.value"
            @click="formModel.creation_type = t.value"
          >
            <span class="type-name">{{ t.label }}</span>
            <span class="type-desc">{{ t.desc }}</span>
          </button>
        </div>
      </n-form-item>
      <div class="style-section">
        <button
          type="button"
          class="style-toggle"
          :class="{ open: showStyleConfig }"
          :aria-expanded="showStyleConfig"
          @click="toggleStyleConfig(!showStyleConfig)"
        >
          <span class="toggle-title">剧本类型与风格配置</span>
          <span class="toggle-hint">可选。不设置则使用默认风格，创建后也可在剧本页调整。</span>
          <span class="toggle-arrow">{{ showStyleConfig ? '收起' : '展开' }}</span>
        </button>
        <div v-if="showStyleConfig" class="style-body">
          <div v-if="!styleOptions" class="style-loading">正在加载风格选项…</div>
          <StyleConfigForm v-else-if="styleConfig" v-model="styleConfig" :options="styleOptions" />
        </div>
      </div>
    </n-form>

    <!-- ===== 系列表单 ===== -->
    <n-form
      v-else
      ref="seriesFormRef"
      :model="seriesForm"
      :rules="seriesRules"
      label-placement="top"
      :show-require-mark="false"
    >
      <n-form-item label="系列名称" path="name">
        <n-input
          v-model:value="seriesForm.name"
          placeholder="例如：小熊成长记"
          maxlength="200"
          show-count
        />
      </n-form-item>
      <n-form-item label="系列简介" path="description">
        <n-input
          v-model:value="seriesForm.description"
          type="textarea"
          :autosize="{ minRows: 3, maxRows: 6 }"
          placeholder="简述系列的主题、世界观与整体走向"
          maxlength="2000"
          show-count
        />
      </n-form-item>
      <div class="form-row">
        <n-form-item label="题材类型" path="genre" class="form-row-item">
          <n-select
            v-model:value="seriesForm.genre"
            :options="seriesGenreOptions"
            placeholder="选择题材类型"
            filterable
            clearable
          />
        </n-form-item>
        <n-form-item label="目标受众" path="target_audience" class="form-row-item">
          <n-select
            v-model:value="seriesForm.target_audience"
            :options="seriesAudienceOptions"
            placeholder="选择目标受众"
            filterable
            clearable
          />
        </n-form-item>
      </div>
      <n-form-item label="整体风格" path="style">
        <div class="series-style-field">
          <n-select
            v-model:value="seriesForm.style"
            :options="seriesStyleOptions"
            placeholder="选择整体视觉风格"
            filterable
            clearable
          />
          <div v-if="seriesStyleHint" class="series-style-hint">{{ seriesStyleHint }}</div>
        </div>
      </n-form-item>
    </n-form>

    <template #footer>
      <div class="modal-footer">
        <n-button @click="onUpdateShow(false)">取消</n-button>
        <n-button
          v-if="createType === 'project'"
          type="primary"
          :loading="creating"
          @click="handleCreate"
        >创建作品</n-button>
        <n-button
          v-else
          type="primary"
          :loading="creatingSeries"
          @click="handleCreateSeries"
        >创建系列</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
/* ===== 创建类型选择卡 ===== */
.create-type-cards {
  display: flex;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}
.create-type-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-1);
  padding: 14px;
  text-align: left;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  cursor: pointer;
  color: var(--text-primary);
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease;
}
.create-type-card:hover {
  border-color: var(--border-strong);
}
.create-type-card.active {
  border-color: var(--brand-primary);
  background: var(--brand-soft);
}
.create-type-card:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
.ct-icon {
  font-size: 22px;
  line-height: 1;
}
.ct-name {
  margin-top: 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}
.ct-desc {
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
}
/* ===== 画幅比 ===== */
.ratio-cards {
  display: flex;
  gap: var(--sp-3);
  width: 100%;
}
.ratio-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  cursor: pointer;
  color: var(--text-primary);
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease;
}
.ratio-card:hover {
  border-color: var(--border-strong);
}
.ratio-card.active {
  border-color: var(--brand-primary);
  background: var(--brand-soft);
}
.ratio-card:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
.ratio-shape {
  display: block;
  border-radius: var(--radius);
  background: var(--brand-primary);
}
.shape-9-16 {
  width: 18px;
  height: 32px;
}
.shape-16-9 {
  width: 32px;
  height: 18px;
}
.shape-1-1 {
  width: 26px;
  height: 26px;
}
.ratio-label {
  font-size: 13px;
  line-height: 20px;
}
/* ===== 创作类型 ===== */
.type-cards {
  display: flex;
  gap: var(--sp-3);
  width: 100%;
}
.type-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--sp-1);
  padding: var(--sp-3);
  text-align: left;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  cursor: pointer;
  color: var(--text-primary);
  transition:
    border-color 0.2s ease,
    background-color 0.2s ease;
}
.type-card:hover {
  border-color: var(--border-strong);
}
.type-card.active {
  border-color: var(--brand-primary);
  background: var(--brand-soft);
}
.type-card:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
.type-name {
  font-size: 14px;
  line-height: 22px;
  font-weight: 500;
}
.type-desc {
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
}
/* ===== 风格配置折叠区 ===== */
.style-section {
  margin-top: var(--sp-1);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border-subtle);
}
.style-toggle {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--sp-2);
  width: 100%;
  padding: 10px var(--sp-3);
  border-radius: var(--radius-lg);
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s;
}
.style-toggle:hover,
.style-toggle.open {
  border-color: var(--brand-primary);
}
.style-toggle:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}
.toggle-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}
.toggle-hint {
  flex: 1 1 auto;
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
}
.toggle-arrow {
  font-size: 12px;
  color: var(--brand-primary);
  white-space: nowrap;
}
.style-body {
  margin-top: var(--sp-3);
  max-height: 46vh;
  overflow-y: auto;
  padding-right: var(--sp-1);
}
.style-loading {
  font-size: 13px;
  color: var(--text-secondary);
  padding: var(--sp-3) 0;
}
/* ===== 系列表单两列 ===== */
.form-row {
  display: flex;
  gap: var(--sp-3);
}
.form-row-item {
  flex: 1;
  min-width: 0;
}
.series-style-field {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.series-style-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-3);
}
</style>
