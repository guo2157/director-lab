<script setup lang="ts">
/**
 * 校验补充结果弹窗：原文 / 优化稿对比、变更清单、守恒校验、待补要素。
 * 采用与保留的写回逻辑在页面里，本组件只负责展示与抛意图。
 */
import { NButton, NModal, NTag } from 'naive-ui'
import type { ScriptNormalizeResult } from '@/api/projects'

defineProps<{
  show: boolean
  originalText: string
  result: ScriptNormalizeResult | null
}>()

const emit = defineEmits<{
  'update:show': [boolean]
  adopt: []
  keep: []
  force: []
}>()

function changeLabel(cat: string): string {
  if (cat === 'format') return '格式'
  if (cat === 'clarity') return '明确化'
  if (cat === 'completion') return '补全'
  return cat
}
function changeTagType(cat: string): 'default' | 'info' | 'warning' {
  if (cat === 'completion') return 'warning'
  if (cat === 'clarity') return 'info'
  return 'default'
}
</script>

<template>
  <n-modal
    :show="show"
    preset="card"
    title="剧本校验补充"
    :bordered="false"
    style="width: min(1100px, 96vw)"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div v-if="result" class="compare">
      <div class="compare-summary">
        <NTag size="small" :type="result.checks.blocked ? 'error' : 'success'">
          {{ result.checks.blocked ? '守恒校验未通过' : '守恒校验通过' }}
        </NTag>
        <span>处理层级：第 {{ result.level }} 层</span>
        <span>{{ result.original_chars }} → {{ result.normalized_chars }} 字</span>
      </div>

      <div v-if="result.checks.blocked" class="blocked-tip">
        <div v-if="result.checks.missing_characters?.length">
          丢失人物：{{ result.checks.missing_characters.join('、') }}
        </div>
        <div v-if="result.checks.missing_dialogues?.length">
          丢失台词：{{ result.checks.missing_dialogues.join(' / ') }}
        </div>
        <div>优化稿缺失了上述人物或台词。若这是你想要的删改，可点右下角「仍然采用」强制替换。</div>
      </div>
      <div v-else-if="result.checks.punct_warnings?.length" class="punct-tip">
        <div>以下台词仅有标点或空白差异，不影响采用：</div>
        <div class="punct-list">{{ result.checks.punct_warnings.join(' / ') }}</div>
      </div>

      <div class="compare-body">
        <div class="compare-pane">
          <div class="pane-title">原文</div>
          <pre class="pane-text">{{ originalText }}</pre>
        </div>
        <div class="compare-pane">
          <div class="pane-title">优化稿</div>
          <pre class="pane-text">{{ result.normalized_text }}</pre>
        </div>
      </div>

      <div v-if="result.changes.length" class="changes">
        <div class="changes-title">变更清单（{{ result.changes.length }}）</div>
        <div v-for="(c, i) in result.changes" :key="i" class="change-item">
          <NTag size="tiny" :type="changeTagType(c.category)">{{ changeLabel(c.category) }}</NTag>
          <span class="change-summary">{{ c.summary }}</span>
          <NTag v-if="c.ai_generated" size="tiny" type="warning">AI 补全</NTag>
          <span v-if="c.evidence" class="change-evidence">依据：{{ c.evidence }}</span>
        </div>
      </div>
      <div v-else class="changes-empty">未发现需要调整的内容，格式已符合规范</div>

      <div v-if="result.questions.length" class="questions">
        <div class="changes-title">需要你补充的关键要素</div>
        <ul>
          <li v-for="(q, i) in result.questions" :key="i">{{ q }}</li>
        </ul>
        <div class="questions-tip">补充后可修改原文并再次点『校验补充』，获得更贴合原意的优化稿</div>
      </div>
    </div>

    <template #footer>
      <div class="modal-footer">
        <NButton @click="emit('update:show', false)">稍后处理</NButton>
        <NButton @click="emit('keep')">保留原文</NButton>
        <NButton v-if="!result?.checks.blocked" type="primary" :disabled="!result" @click="emit('adopt')">
          采用优化稿
        </NButton>
        <NButton v-else type="warning" secondary @click="emit('force')">仍然采用</NButton>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.compare { display: flex; flex-direction: column; gap: 12px; max-height: 64vh; overflow-y: auto; }
.compare-summary { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--text-secondary); flex-wrap: wrap; }
.blocked-tip { padding: 8px 10px; border-radius: 8px; background: var(--danger-soft); color: var(--state-danger); font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
/* 标点/空白差异提示：不阻断采用，用琥珀色与红色阻断区分 */
.punct-tip { padding: 8px 10px; border-radius: 8px; background: var(--warning-soft); color: var(--state-warning); font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
.punct-list { color: var(--text-secondary); word-break: break-all; }
.compare-body { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.compare-pane { display: flex; flex-direction: column; min-width: 0; border: 1px solid var(--border-subtle); border-radius: 8px; overflow: hidden; }
.pane-title { padding: 6px 10px; font-size: 12px; font-weight: 600; color: var(--text-primary); background: var(--bg-elevated); border-bottom: 1px solid var(--border-subtle); }
.pane-text { margin: 0; padding: 10px; font-family: var(--font-mono); font-size: 12.5px; line-height: 1.7; color: var(--text-primary); white-space: pre-wrap; word-break: break-word; max-height: 40vh; overflow-y: auto; }
.changes { display: flex; flex-direction: column; gap: 6px; }
.changes-title { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.change-item { display: flex; align-items: baseline; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.change-summary { color: var(--text-primary); }
.change-evidence { color: var(--text-tertiary); font-size: 11px; }
.changes-empty { font-size: 12px; color: var(--text-tertiary); padding: 6px 0; }
.questions { display: flex; flex-direction: column; gap: 4px; padding: 10px; border-radius: 8px; background: var(--warning-soft); }
.questions ul { margin: 0; padding-left: 18px; font-size: 12px; color: var(--state-warning); }
.questions-tip { font-size: 11px; color: var(--text-tertiary); }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; }

@media (max-width: 768px) {
  .compare-body { grid-template-columns: 1fr; }
  .pane-text { max-height: 28vh; }
}
</style>
