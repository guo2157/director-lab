<script setup lang="ts">
/**
 * 项目默认工作流配置面板（G2，v2 §11.3 / §19）。
 *
 * 设计要点：
 * - 每个业务槽位（角色/场景/道具/分镜图/分镜视频/配音/BGM/音效）独立选择，空 = 用系统默认。
 * - 候选的 `usable` 与 `missing` 由后端按槽位必需能力算好，前端只负责展示与禁用——
 *   不在这里二次推断能力，避免前后端两套判断逻辑漂移。
 * - 不兼容的工作流**不静默回退**：能选就选，但明确标出缺什么能力；真正生成时后端会 422。
 * - 保存后不自动重跑任何任务，只影响后续生成。
 */
import { computed, ref, watch } from 'vue'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { NAlert, NButton, NSelect, NSpin, useMessage } from 'naive-ui'
import { getProjectWorkflowConfig, saveProjectWorkflowConfig } from '@/api/projects'
import type { WorkflowCandidate } from '@/api/projects'

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ (e: 'saved'): void }>()

const message = useMessage()
const queryClient = useQueryClient()

/** 槽位中文名 + 用途说明：让用户知道「这一项影响哪个页面/哪一步」。
 *  槽位键必须与后端 matcher.SLOT_WF_TYPE 完全一致（image / storyboard_image /
 *  video / tts / bgm / sfx），前端不另造业务名，避免保存时被后端拒为未知槽位。 */
const SLOT_META: Record<string, { label: string; hint: string }> = {
  image: { label: '公共资产图', hint: '资产库 → 角色定妆图 / 场景基准图 / 道具锚定图' },
  storyboard_image: { label: '分镜图', hint: '分镜图页单张/批量出图' },
  video: { label: '分镜视频', hint: '分镜看板 → 镜头视频生成（H3）' },
  tts: { label: '配音', hint: '音频模块 → 对白/旁白生成' },
  bgm: { label: '背景音乐', hint: '音频模块 → BGM 生成' },
  sfx: { label: '音效', hint: '音频模块 → 音效生成' },
}

const configQuery = useQuery({
  queryKey: computed(() => ['project-workflow-config', props.projectId]),
  queryFn: () => getProjectWorkflowConfig(props.projectId),
  enabled: computed(() => !!props.projectId),
})

/** 表单：slot → workflow_id（'' 表示恢复系统默认） */
const form = ref<Record<string, string>>({})
watch(
  () => configQuery.data.value,
  (data) => {
    if (!data) return
    const next: Record<string, string> = {}
    for (const slot of data.slots) next[slot] = data.defaults[slot] ?? ''
    form.value = next
  },
  { immediate: true },
)

/** 展示顺序：先出图（公共资产 → 分镜图），再视频，最后音频三类 */
const SLOT_ORDER = ['image', 'storyboard_image', 'video', 'tts', 'bgm', 'sfx']

const slots = computed(() => {
  const data = configQuery.data.value
  if (!data) return []
  const known = SLOT_ORDER.filter((s) => data.slots.includes(s))
  const rest = data.slots.filter((s) => !SLOT_ORDER.includes(s)).sort()
  return [...known, ...rest]
})

function candidatesFor(slot: string): WorkflowCandidate[] {
  return configQuery.data.value?.candidates?.[slot] ?? []
}

/** 下拉选项：不可用的项仍然列出（用户可能想看到为什么不能用），但带明确标注 */
function optionsFor(slot: string) {
  const rows = candidatesFor(slot)
  const opts = rows.map((c) => {
    const flags: string[] = []
    if (c.is_default) flags.push('系统默认')
    if (c.tier) flags.push(c.tier)
    if (!c.usable) flags.push(`缺 ${c.missing.join('/')}`)
    return {
      label: `${c.name} · v${c.version}${flags.length ? ' · ' + flags.join(' / ') : ''}`,
      value: c.id,
      disabled: !c.usable,
      candidate: c,
    }
  })
  return [{ label: '跟随系统默认（不指定）', value: '' }, ...opts]
}

/** 当前选中项若不可用，给出显式警告（不阻止保存，但让用户知道后果） */
function selectionWarning(slot: string): string {
  const id = form.value[slot]
  if (!id) return ''
  const c = candidatesFor(slot).find((x) => x.id === id)
  if (!c) return '所选工作流已不存在，保存后该槽位将回退系统默认'
  if (!c.usable) {
    return `该工作流缺少槽位必需能力（${c.missing.join('、')}），生成时会明确报错而不会自动换用其他工作流`
  }
  return ''
}

const dirty = computed(() => {
  const origin = configQuery.data.value?.defaults ?? {}
  const keys = new Set([...Object.keys(origin), ...Object.keys(form.value)])
  for (const k of keys) {
    if ((origin[k] ?? '') !== (form.value[k] ?? '')) return true
  }
  return false
})

const saveMutation = useMutation({
  mutationFn: (defaults: Record<string, string>) =>
    saveProjectWorkflowConfig(props.projectId, defaults),
  onSuccess: (res) => {
    const next: Record<string, string> = {}
    for (const slot of slots.value) next[slot] = res.defaults[slot] ?? ''
    form.value = next
    message.success('项目默认工作流已保存，后续生成按新配置执行')
    void queryClient.invalidateQueries({ queryKey: ['project-workflow-config', props.projectId] })
    emit('saved')
  },
  onError: (e: unknown) => {
    const msg = e instanceof Error ? e.message : String(e)
    message.error(`保存失败：${msg}`)
  },
})

function save() {
  // 空值也要提交：后端把空字符串视为「移除该槽位 → 恢复系统默认」
  saveMutation.mutate({ ...form.value })
}

function resetSlot(slot: string) {
  form.value = { ...form.value, [slot]: '' }
}

const loading = computed(() => configQuery.isLoading.value)
const loadError = computed(() => {
  const e = configQuery.error.value
  if (!e) return ''
  return e instanceof Error ? e.message : String(e)
})
</script>

<template>
  <section class="wf-config-panel">
    <div v-if="loading" class="panel-loading"><NSpin size="small" /> 正在加载工作流配置…</div>
    <NAlert v-else-if="loadError" type="error" :bordered="false">加载失败：{{ loadError }}</NAlert>

    <template v-else>
      <NAlert type="info" :bordered="false" class="panel-note">
        每个槽位可指定本项目的默认工作流；留空则跟随系统默认。项目默认<b>不绕过能力校验</b>——
        选中不满足槽位能力的工作流时，生成会明确报错并列出缺失能力，不会静默换用其他工作流。
      </NAlert>

      <div class="slot-list">
        <div v-for="slot in slots" :key="slot" class="slot-row">
          <div class="slot-head">
            <span class="slot-label">{{ SLOT_META[slot]?.label ?? slot }}</span>
            <span class="slot-hint">{{ SLOT_META[slot]?.hint ?? '' }}</span>
          </div>
          <div class="slot-control">
            <NSelect
              v-model:value="form[slot]"
              :options="optionsFor(slot)"
              size="small"
              filterable
              :placeholder="candidatesFor(slot).length ? '跟随系统默认' : '该槽位暂无可用工作流'"
              :consistent-menu-width="false"
            />
            <NButton
              v-if="form[slot]"
              size="tiny"
              quaternary
              class="slot-reset"
              @click="resetSlot(slot)"
            >恢复默认</NButton>
          </div>
          <NAlert
            v-if="selectionWarning(slot)"
            type="warning"
            :bordered="false"
            class="slot-warn"
          >
            {{ selectionWarning(slot) }}
          </NAlert>
          <div v-else-if="!candidatesFor(slot).length" class="slot-empty">
            还没有该类型的工作流，请先到「设置 → 工作流」上传
          </div>
        </div>
      </div>

      <div class="footer">
        <span v-if="dirty" class="dirty-hint">有未保存的修改</span>
        <span v-else class="saved-hint">已与保存值一致</span>
        <NButton
          type="primary"
          size="small"
          :loading="saveMutation.isPending.value"
          :disabled="!dirty"
          @click="save"
        >
          保存工作流配置
        </NButton>
      </div>
      <div class="footer-note">保存只影响后续新任务；已生成的资产与分镜不会被自动重做。</div>
    </template>
  </section>
</template>

<style scoped>
.wf-config-panel { display: flex; flex-direction: column; gap: 14px; padding: 4px 2px 8px; }
.panel-loading { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary); padding: 12px 0; }
.panel-note { font-size: 12px; line-height: 1.7; }
.panel-note b { font-weight: 600; }
.slot-list { display: flex; flex-direction: column; gap: 10px; }
.slot-row {
  display: flex; flex-direction: column; gap: 6px;
  padding: 10px 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg);
  background: var(--bg-elevated);
}
.slot-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.slot-label { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.slot-hint { font-size: 11px; color: var(--text-tertiary); }
.slot-control { display: flex; align-items: center; gap: 8px; }
.slot-control :deep(.n-select) { flex: 1 1 auto; min-width: 0; }
.slot-reset { flex: 0 0 auto; }
.slot-warn { font-size: 11px; line-height: 1.6; }
.slot-empty { font-size: 11px; color: var(--text-tertiary); }
.footer { display: flex; align-items: center; justify-content: flex-end; gap: 10px; padding-top: 4px; }
.dirty-hint { font-size: 12px; color: var(--brand-warning); }
.saved-hint { font-size: 12px; color: var(--text-tertiary); }
.footer-note { font-size: 11px; color: var(--text-tertiary); text-align: right; }
</style>
