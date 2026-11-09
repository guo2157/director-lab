<script setup lang="ts">
import { computed, ref, watch } from "vue"
import { useQuery, useMutation, useQueryClient } from "@tanstack/vue-query"
import { NButton, NSpin, NAlert, useMessage } from "naive-ui"
import { getStyleOptions, getProjectStyleConfig, saveProjectStyleConfig } from "@/api/projects"
import type { StyleConfig, StyleIssue } from "@/types/project"
import StyleConfigForm from "@/components/StyleConfigForm.vue"

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ (e: "saved"): void }>()

const message = useMessage()
const queryClient = useQueryClient()

const optionsQuery = useQuery({
  queryKey: ["style-options"],
  queryFn: () => getStyleOptions(),
  staleTime: 5 * 60 * 1000,
})

const configQuery = useQuery({
  queryKey: computed(() => ["style-config", props.projectId]),
  queryFn: () => getProjectStyleConfig(props.projectId),
  enabled: computed(() => !!props.projectId),
})

const form = ref<StyleConfig | null>(null)
const issues = ref<StyleIssue[]>([])

watch(
  () => configQuery.data.value,
  (data) => {
    if (!data) return
    form.value = { ...data.config }
    issues.value = data.issues ?? []
  },
  { immediate: true },
)

const options = computed(() => optionsQuery.data.value)
const loading = computed(() => configQuery.isLoading.value || optionsQuery.isLoading.value)
const loadError = computed(() => {
  const e = configQuery.error.value ?? optionsQuery.error.value
  if (!e) return ""
  return e instanceof Error ? e.message : String(e)
})

const saveMutation = useMutation({
  mutationFn: (cfg: StyleConfig) => saveProjectStyleConfig(props.projectId, cfg),
  onSuccess: (res) => {
    form.value = { ...res.config }
    issues.value = res.issues ?? []
    message.success("风格配置已保存，后续任务将按新配置执行")
    void queryClient.invalidateQueries({ queryKey: ["style-config", props.projectId] })
    void queryClient.invalidateQueries({ queryKey: ["project", props.projectId] })
    emit("saved")
  },
  onError: (e: unknown) => {
    const msg = e instanceof Error ? e.message : String(e)
    message.error(`保存失败：${msg}`)
  },
})

function save() {
  if (!form.value) return
  saveMutation.mutate({ ...form.value })
}

/* 有未保存改动时提示，避免用户以为已生效 */
const dirty = computed(() => {
  const origin = configQuery.data.value?.config
  if (!origin || !form.value) return false
  return JSON.stringify(origin) !== JSON.stringify(form.value)
})
</script>

<template>
  <section class="style-panel">
    <div v-if="loading" class="panel-loading"><NSpin size="small" /> 正在加载风格配置…</div>
    <NAlert v-else-if="loadError" type="error" :bordered="false">
      加载失败：{{ loadError }}
    </NAlert>

    <template v-else-if="form && options">
      <NAlert
        v-for="issue in issues.filter((i) => i.severity === 'error')"
        :key="issue.code + issue.message"
        type="error"
        :bordered="false"
        class="issue"
      >
        {{ issue.message }}
      </NAlert>
      <NAlert
        v-for="issue in issues.filter((i) => i.severity !== 'error')"
        :key="issue.code + issue.message"
        type="warning"
        :bordered="false"
        class="issue"
      >
        {{ issue.message }}
      </NAlert>

      <StyleConfigForm v-model="form" :options="options" />

      <div class="footer">
        <span v-if="dirty" class="dirty-hint">有未保存的修改</span>
        <span v-else class="saved-hint">已与保存值一致</span>
        <NButton type="primary" size="small" :loading="saveMutation.isPending.value" @click="save">
          保存风格配置
        </NButton>
      </div>
      <div class="footer-note">保存只影响后续新任务；已审核资产与已生成分镜不会被自动重做。</div>
    </template>
  </section>
</template>

<style scoped>
.style-panel { display: flex; flex-direction: column; gap: 14px; padding: 4px 2px 8px; }
.panel-loading { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); padding: 12px 0; }
.issue { margin-bottom: 2px; }
.block { display: flex; flex-direction: column; gap: 6px; }
.block-title { font-size: 13px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
.block-title em { font-style: normal; font-size: 11px; font-weight: 500; padding: 0 6px; border-radius: 8px; }
.required { color: #fff; background: var(--brand-primary); }
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
.style-card:hover { border-color: var(--brand-primary); }
.style-card.active { border-color: var(--brand-primary); box-shadow: 0 0 0 2px rgba(53, 179, 166, 0.15); }
.card-label { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.card-desc { font-size: 11px; color: var(--text-secondary); line-height: 1.5; }
.counter { font-size: 11px; color: var(--text-tertiary); text-align: right; }
.footer { display: flex; align-items: center; justify-content: flex-end; gap: 10px; padding-top: 4px; }
.dirty-hint { font-size: 12px; color: var(--brand-warning, #f0a020); }
.saved-hint { font-size: 12px; color: var(--text-tertiary); }
.footer-note { font-size: 11px; color: var(--text-tertiary); text-align: right; }
</style>
