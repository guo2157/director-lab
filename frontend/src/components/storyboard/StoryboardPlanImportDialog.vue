<script setup lang="ts">
/**
 * 分镜规划导入弹窗（Phase 2）：粘贴规划 JSON → 校验预览 → 显式写入。
 *
 * 校验只读；写入为安全 upsert：只写规划显式字段、锁定镜头关键字段不覆盖、
 * 规划外旧镜头不删除、不重建资产计划、不触发生成。必须先校验通过才能写入。
 */
import { computed, ref, watch } from 'vue'
import { NAlert, NButton, NInput, NModal, NSpace, NTag, useMessage } from 'naive-ui'
import { ApiError } from '@/api/client'
import { getCurrentScript } from '@/api/projects'
import { applyStoryboardPlan, validateStoryboardPlan } from '@/api/storyboardPlan'
import type {
  StoryboardPlanApplyResult,
  StoryboardPlanIssue,
  StoryboardPlanValidateResult,
} from '@/api/storyboardPlan'

const props = defineProps<{
  show: boolean
  projectId: string
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  applied: []
}>()

const message = useMessage()

const planText = ref('')
const parseError = ref('')
const validating = ref(false)
const applying = ref(false)
const preview = ref<StoryboardPlanValidateResult | null>(null)
const result = ref<StoryboardPlanApplyResult | null>(null)
const applyIssues = ref<StoryboardPlanIssue[]>([])

const errorIssues = computed(
  () => preview.value?.issues.filter((item) => item.level === 'error') ?? [],
)
const warningIssues = computed(
  () => preview.value?.issues.filter((item) => item.level === 'warning') ?? [],
)
const canApply = computed(() => preview.value?.valid === true && !applying.value)

watch(planText, () => {
  preview.value = null
  result.value = null
  parseError.value = ''
  applyIssues.value = []
})

function close() {
  emit('update:show', false)
}

async function resolvePlan(): Promise<{ scriptVersionId: string; plan: unknown }> {
  let plan: unknown
  try {
    plan = JSON.parse(planText.value)
  } catch (e) {
    throw new Error(`JSON 解析失败：${e instanceof Error ? e.message : String(e)}`)
  }
  const current = await getCurrentScript(props.projectId)
  if (!current.script_version_id) {
    throw new Error('当前项目没有剧本版本，请先保存剧本')
  }
  return { scriptVersionId: current.script_version_id, plan }
}

async function runValidate() {
  validating.value = true
  result.value = null
  applyIssues.value = []
  try {
    const { scriptVersionId, plan } = await resolvePlan()
    preview.value = await validateStoryboardPlan(props.projectId, scriptVersionId, plan)
    parseError.value = ''
  } catch (e) {
    preview.value = null
    parseError.value = e instanceof Error ? e.message : '校验失败'
  } finally {
    validating.value = false
  }
}

async function runApply() {
  if (!preview.value?.valid) return
  applying.value = true
  applyIssues.value = []
  try {
    const { scriptVersionId, plan } = await resolvePlan()
    result.value = await applyStoryboardPlan(props.projectId, scriptVersionId, plan)
    parseError.value = ''
    message.success(
      `规划已写入：新增 ${result.value.created_shot_ids.length}、更新 ${result.value.updated_shot_ids.length}`,
    )
    emit('applied')
  } catch (e) {
    parseError.value = e instanceof Error ? e.message : '写入失败'
    if (e instanceof ApiError && e.detail && typeof e.detail === 'object') {
      const issues = (e.detail as { issues?: StoryboardPlanIssue[] }).issues
      if (Array.isArray(issues)) applyIssues.value = issues
    }
  } finally {
    applying.value = false
  }
}

function issueText(issue: StoryboardPlanIssue): string {
  return `${issue.path || '-'}：${issue.message}`
}

/** Shot 列名 → 中文标签；与后端 `_write_fields` 的写入列一致 */
const FIELD_LABELS: Record<string, string> = {
  seq: '序号',
  duration: '时长',
  shot_kind: '镜头类型',
  shot_type: '景别',
  camera_angle: '机位',
  camera_movement: '运镜',
  dialogue: '对白',
  shot_purpose: '镜头目的',
  emotion: '情绪',
  soundscape: '声音',
  start_state: '起始状态',
  end_state: '结束状态',
  character_id: '角色',
  props: '道具',
  beats: '节拍',
  image_prompt: '分镜提示词',
}

/** 差异值展示：空值显式标注，长文本截断，避免撑破弹窗 */
function formatChangeValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '空'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > 36 ? `${text.slice(0, 36)}…` : text
}

</script>

<template>
  <n-modal
    :show="props.show"
    preset="card"
    title="导入分镜规划"
    closable
    :bordered="false"
    :style="{ width: 'min(920px, 94vw)' }"
    :content-style="{ maxHeight: 'calc(88vh - 56px)', overflowY: 'auto' }"
    @update:show="close"
  >
    <n-space vertical :size="12">
      <n-alert type="info" :bordered="false">
        粘贴分镜规划 JSON。校验只读；写入仅做安全 upsert，不覆盖已锁定镜头的
        seq/duration/shot_kind，不删除规划外的旧镜头，不重建资产计划，不触发生成。
      </n-alert>

      <n-input
        v-model:value="planText"
        type="textarea"
        placeholder='{"plan_version": 1, "shots": [{"shot_id": "sht_001", "scene_id": "scn_...", "sequence": 1, "duration": 4, "shot_kind": "visual", "visual_description": "…", "source_block_ids": ["blk_..."]}]}'
        :autosize="{ minRows: 10, maxRows: 22 }"
        class="plan-input"
      />

      <n-space align="center">
        <n-button :loading="validating" secondary @click="runValidate">校验预览</n-button>
        <n-button type="primary" :loading="applying" :disabled="!canApply" @click="runApply">
          写入分镜
        </n-button>
        <span class="plan-hint">
          {{ preview?.valid ? '校验通过，可写入' : '先校验，通过后才能写入' }}
        </span>
      </n-space>

      <n-alert v-if="parseError" type="error" :bordered="false" closable @close="parseError = ''">
        {{ parseError }}
      </n-alert>

      <div v-if="preview" class="plan-preview">
        <n-space align="center" :size="8" class="plan-tags">
          <n-tag :type="preview.valid ? 'success' : 'error'" size="small">
            {{ preview.valid ? '校验通过' : '校验未通过' }}
          </n-tag>
          <n-tag size="small" type="error" :bordered="false">错误 {{ errorIssues.length }}</n-tag>
          <n-tag size="small" type="warning" :bordered="false">警告 {{ warningIssues.length }}</n-tag>
          <n-tag size="small" :bordered="false">
            场景覆盖 {{ preview.coverage.scenes.covered }}/{{ preview.coverage.scenes.total }}
          </n-tag>
          <n-tag size="small" :bordered="false">
            节拍覆盖 {{ preview.coverage.beats.covered }}/{{ preview.coverage.beats.total }}
          </n-tag>
          <n-tag size="small" :bordered="false">
            镜头 {{ preview.adapted_shots.length }} · 共 {{ preview.comparison.duration_total_seconds }}s
          </n-tag>
        </n-space>

        <ul v-if="preview.issues.length" class="plan-issues">
          <li
            v-for="(issue, index) in preview.issues"
            :key="`${issue.code}-${index}`"
            :class="['plan-issue', `is-${issue.level}`]"
          >
            <span class="plan-issue-level">{{ issue.level === 'error' ? '错误' : issue.level === 'warning' ? '警告' : '提示' }}</span>
            <span class="plan-issue-text">{{ issueText(issue) }}</span>
          </li>
        </ul>

        <div class="plan-compare">
          <div v-if="preview.comparison.reused_shot_ids.length">
            复用既有镜头：{{ preview.comparison.reused_shot_ids.join('、') }}
          </div>
          <div v-if="preview.comparison.locked_conflicts.length">
            锁定镜头冲突（写入时跳过）：
            {{ preview.comparison.locked_conflicts.map((item) => `${item.shot_id}（${item.fields.join('/')}）`).join('、') }}
          </div>
          <div v-if="preview.comparison.removed_shot_ids.length">
            规划外旧镜头（不会删除）：{{ preview.comparison.removed_shot_ids.join('、') }}
          </div>
        </div>

        <div v-if="preview.comparison.field_changes.length" class="plan-changes">
          <div class="plan-changes-title">
            写入将改动 {{ preview.comparison.field_changes.length }} 个既有镜头的字段
          </div>
          <ul class="plan-change-list">
            <li
              v-for="group in preview.comparison.field_changes"
              :key="group.shot_id"
              class="plan-change-row"
            >
              <span class="plan-change-shot">{{ group.shot_id }}</span>
              <n-tag v-if="group.will_skip" size="tiny" type="warning" :bordered="false">
                已锁定，写入时跳过
              </n-tag>
              <span class="plan-change-fields">
                <span
                  v-for="change in group.fields"
                  :key="change.field"
                  class="plan-change-field"
                >
                  {{ FIELD_LABELS[change.field] ?? change.field }}
                  <span class="plan-change-from">{{ formatChangeValue(change.from) }}</span>
                  →
                  <span class="plan-change-to">{{ formatChangeValue(change.to) }}</span>
                </span>
              </span>
            </li>
          </ul>
        </div>
      </div>

      <ul v-if="applyIssues.length" class="plan-issues">
        <li
          v-for="(issue, index) in applyIssues"
          :key="`apply-${issue.code}-${index}`"
          :class="['plan-issue', `is-${issue.level}`]"
        >
          <span class="plan-issue-level">{{ issue.level === 'error' ? '错误' : issue.level === 'warning' ? '警告' : '提示' }}</span>
          <span class="plan-issue-text">{{ issueText(issue) }}</span>
        </li>
      </ul>

      <n-alert v-if="result?.applied" type="success" :bordered="false">
        <div class="plan-result-title">写入完成</div>
        <div>新增镜头 {{ result.created_shot_ids.length }} 个<span v-if="result.created_shot_ids.length">：{{ result.created_shot_ids.join('、') }}</span></div>
        <div>更新镜头 {{ result.updated_shot_ids.length }} 个<span v-if="result.updated_shot_ids.length">：{{ result.updated_shot_ids.join('、') }}</span></div>
        <div v-if="result.unchanged_shot_ids.length">无变化 {{ result.unchanged_shot_ids.length }} 个：{{ result.unchanged_shot_ids.join('、') }}</div>
        <div v-if="result.skipped_locked.length">
          跳过锁定镜头 {{ result.skipped_locked.length }} 个：
          {{ result.skipped_locked.map((item) => `${item.shot_id}（${item.fields.join('/')}）`).join('、') }}
        </div>
        <div v-if="result.orphan_shot_ids.length">
          规划外旧镜头 {{ result.orphan_shot_ids.length }} 个（未删除）：{{ result.orphan_shot_ids.join('、') }}
        </div>
        <ul v-if="result.notes?.length" class="plan-notes">
          <li v-for="note in result.notes" :key="note">{{ note }}</li>
        </ul>
      </n-alert>
    </n-space>
  </n-modal>
</template>

<style scoped>
.plan-input :deep(textarea) {
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
}
.plan-hint {
  color: var(--text-tertiary);
  font-size: 12px;
}
.plan-preview {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.plan-tags {
  flex-wrap: wrap;
}
.plan-issues {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 220px;
  overflow-y: auto;
}
.plan-issue {
  display: flex;
  gap: var(--sp-2);
  font-size: 12px;
  line-height: 1.5;
  padding: 4px 8px;
  border-radius: var(--radius);
  background: var(--bg-panel);
}
.plan-issue-level {
  flex: none;
  font-weight: 600;
}
.plan-issue.is-error .plan-issue-level {
  color: var(--danger-text);
}
.plan-issue.is-warning .plan-issue-level {
  color: var(--state-warning);
}
.plan-issue.is-info .plan-issue-level {
  color: var(--text-tertiary);
}
.plan-issue-text {
  color: var(--text-secondary);
  word-break: break-word;
}
.plan-compare {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.plan-changes {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.plan-changes-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}
.plan-change-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
}
.plan-change-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px var(--sp-2);
  padding: 4px 8px;
  border-radius: var(--radius);
  background: var(--bg-panel);
  font-size: 12px;
}
.plan-change-shot {
  flex: none;
  font-family: var(--font-mono);
  color: var(--text-primary);
}
.plan-change-fields {
  display: flex;
  flex-wrap: wrap;
  gap: 4px var(--sp-3);
  color: var(--text-secondary);
}
.plan-change-from {
  color: var(--text-tertiary);
  text-decoration: line-through;
}
.plan-change-to {
  color: var(--text-primary);
}
.plan-result-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.plan-notes {
  margin: 6px 0 0;
  padding-left: 18px;
  color: var(--text-secondary);
  font-size: 12px;
}
</style>
