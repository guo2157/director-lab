<script setup lang="ts">
/**
 * 右栏：任务上下文。
 *
 * 三个折叠面板（项目状态 / 风格基线 / 音频生产），默认只展开「项目状态」，
 * 避免把项目概览、风格基线、音频进度全部平铺造成的拥挤。
 * 待审批生成放在项目状态面板内（同为当前项目的生产动作）。
 */
import { computed, ref } from 'vue'
import { NButton, NCollapse, NCollapseItem, NIcon, NSelect, NTag } from 'naive-ui'
import {
  ChevronForward,
  ColorPaletteOutline,
  FolderOpenOutline,
  LinkOutline,
  MusicalNotesOutline,
  OptionsOutline,
} from '@vicons/ionicons5'
import type { AgentApproval } from '@/api/agent'
import type { StyleConfig, StyleOptions } from '@/types/project'
import AudioProgressPanel from '@/components/AudioProgressPanel.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ApprovalList from './ApprovalList.vue'
import StyleBaselinePanel from './StyleBaselinePanel.vue'
import type { ProjectBrief } from './types'

const props = defineProps<{
  project: ProjectBrief | null
  hasSession: boolean
  projects: ProjectBrief[]
  styleConfig: StyleConfig | null
  styleLabel: string
  styleOptions: StyleOptions | null
  styleLoading: boolean
  approvals: AgentApproval[]
  approvalLoading: boolean
  approvingId: string | null
}>()

const emit = defineEmits<{
  link: [projectId: string | null]
  go: [page: string]
  decide: [approval: AgentApproval, approve: boolean]
  run: [approval: AgentApproval]
  collapse: []
}>()

const linkingMode = ref(false)
const linkTarget = ref<string | null>(null)

const projectOptions = computed(() =>
  props.projects.map((p) => ({ label: p.name, value: p.id })),
)

/** 镜头完成度：仅用于进度条视觉，不参与业务判断 */
const shotPercent = computed(() => {
  const total = props.project?.shot_total ?? 0
  const done = props.project?.shot_done ?? 0
  if (!total) return '0%'
  return `${Math.min(100, Math.round((done / total) * 100))}%`
})

function confirmLink(): void {
  if (!linkTarget.value) return
  emit('link', linkTarget.value)
  linkingMode.value = false
  linkTarget.value = null
}
</script>

<template>
  <aside class="panel context-panel">
    <div class="panel-header">
      <div class="panel-heading">
        <NIcon :component="OptionsOutline" :size="15" class="panel-icon" />
        <span class="panel-title">任务上下文</span>
      </div>
      <NButton
        size="tiny"
        quaternary
        aria-label="收起上下文栏"
        title="收起上下文栏"
        @click="emit('collapse')"
      >
        <template #icon><NIcon :component="ChevronForward" /></template>
      </NButton>
    </div>

    <div class="context-body">
      <NCollapse :default-expanded-names="['project']" arrow-placement="right" class="ctx-collapse">
        <!-- ① 项目状态 -->
        <NCollapseItem name="project">
          <template #header>
            <span class="ctx-item-head">
              <NIcon :component="FolderOpenOutline" :size="14" class="ctx-item-icon" />项目状态
            </span>
          </template>
          <div class="ctx-block">
            <template v-if="!project">
              <EmptyState
                v-if="hasSession"
                compact
                title="未关联项目"
                description="当前会话是普通对话，关联项目后可查看生产进度"
                :action-text="linkingMode ? '' : '关联项目'"
                @action="linkingMode = true"
              >
                <template #icon><NIcon :component="LinkOutline" :size="20" /></template>
              </EmptyState>
              <EmptyState
                v-else
                compact
                title="未选择会话"
                description="先选择或新建会话，再关联项目"
              >
                <template #icon><NIcon :component="LinkOutline" :size="20" /></template>
              </EmptyState>

              <template v-if="linkingMode">
                <NSelect
                  v-model:value="linkTarget"
                  :options="projectOptions"
                  size="small"
                  placeholder="选择项目"
                  aria-label="选择要关联的项目"
                />
                <div class="link-actions">
                  <NButton size="tiny" type="primary" :disabled="!linkTarget" @click="confirmLink">
                    确认
                  </NButton>
                  <NButton size="tiny" @click="linkingMode = false">取消</NButton>
                </div>
              </template>
            </template>

            <template v-else>
              <div class="ctx-project-head">
                <span class="ctx-project-name" :title="project.name">{{ project.name }}</span>
                <NTag size="small" :type="project.status === 'active' ? 'success' : 'default'">
                  {{ project.status === 'active' ? '进行中' : project.status }}
                </NTag>
              </div>
              <div class="ctx-stats">
                <div class="ctx-stat">
                  <span class="ctx-stat-label">当前阶段</span>
                  <span class="ctx-stat-value">{{ project.current_stage }}</span>
                </div>
                <div class="ctx-stat">
                  <span class="ctx-stat-label">镜头进度</span>
                  <span class="ctx-stat-value">
                    {{ project.shot_done }}<span class="ctx-stat-total"> / {{ project.shot_total }}</span>
                  </span>
                  <span class="ctx-progress" aria-hidden="true">
                    <i :style="{ width: shotPercent }"></i>
                  </span>
                </div>
              </div>
              <div class="ctx-links">
                <button type="button" @click="emit('go', 'storyboard')">分镜看板</button>
                <button type="button" @click="emit('go', 'assets')">资产库</button>
                <button type="button" @click="emit('go', 'compose')">合成导出</button>
                <button type="button" class="muted" @click="emit('link', null)">取消关联</button>
              </div>

              <div class="ctx-sub">
                <div class="ctx-sub-head">
                  <span class="ctx-label">待审批生成</span>
                  <span class="ctx-count" :class="{ 'has-items': approvals.length > 0 }">
                    {{ approvals.length }}
                  </span>
                </div>
                <ApprovalList
                  :approvals="approvals"
                  :loading="approvalLoading"
                  :approving-id="approvingId"
                  @decide="(a, approve) => emit('decide', a, approve)"
                  @run="(a) => emit('run', a)"
                />
              </div>
            </template>
          </div>
        </NCollapseItem>

        <!-- ② 风格基线 -->
        <NCollapseItem name="style">
          <template #header>
            <span class="ctx-item-head">
              <NIcon :component="ColorPaletteOutline" :size="14" class="ctx-item-icon" />风格基线
            </span>
          </template>
          <StyleBaselinePanel
            :config="styleConfig"
            :label="styleLabel"
            :options="styleOptions"
            :loading="styleLoading"
            @go="emit('go', $event)"
          />
        </NCollapseItem>

        <!-- ③ 音频生产 -->
        <NCollapseItem name="audio">
          <template #header>
            <span class="ctx-item-head">
              <NIcon :component="MusicalNotesOutline" :size="14" class="ctx-item-icon" />音频生产
            </span>
          </template>
          <div v-if="project" class="ctx-audio">
            <AudioProgressPanel :project-id="project.id" />
          </div>
          <p v-else class="ctx-empty">关联项目后可查看音频生产进度</p>
        </NCollapseItem>
      </NCollapse>
    </div>
  </aside>
</template>

<style scoped>
.panel {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  box-shadow: var(--card-sheen);
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle);
  flex-shrink: 0;
  gap: var(--sp-2);
  min-width: 0;
}

.panel-heading {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
}

.panel-icon {
  color: var(--brand-primary);
  flex-shrink: 0;
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.context-panel {
  width: 320px;
  flex-shrink: 0;
}

.context-body {
  padding: var(--sp-2) var(--sp-3) var(--sp-4);
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

/* 折叠面板：标题行收紧，分隔线沿用令牌色，不引入阴影 */
.ctx-collapse :deep(.n-collapse-item) {
  border-top: 1px solid var(--border-subtle);
}

.ctx-collapse :deep(.n-collapse-item:first-child) {
  border-top: none;
}

.ctx-collapse :deep(.n-collapse-item__header) {
  padding: var(--sp-2) 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.ctx-item-head {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
}

.ctx-item-icon {
  color: var(--text-tertiary);
  transition: color var(--transition-fast);
}

.ctx-collapse :deep(.n-collapse-item__header:hover) {
  color: var(--brand-hover);
}

.ctx-collapse :deep(.n-collapse-item__header:hover) .ctx-item-icon {
  color: var(--brand-primary);
}

.ctx-collapse :deep(.n-collapse-item__content-inner) {
  padding-top: var(--sp-1);
}

.ctx-block {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

/* 音频面板嵌入折叠项：去掉自身卡片外壳与重复标题，只保留刷新入口 */
.ctx-audio :deep(.n-card) {
  background: transparent;
  border: none;
  padding: 0;
}

.ctx-audio :deep(.n-card__content) {
  padding: 0;
}

.ctx-audio :deep(.n-card-header) {
  padding: 0 0 var(--sp-1);
  border-bottom: none;
}

.ctx-audio :deep(.n-card-header__main) {
  display: none;
}

.ctx-project-head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
}

.ctx-project-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 关键指标：两块下沉小卡，让阶段与进度从正文里跳出来 */
.ctx-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--sp-2);
}

.ctx-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-2);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  min-width: 0;
}

.ctx-stat-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.ctx-stat-value {
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ctx-stat-total {
  font-size: 12px;
  font-weight: 400;
  color: var(--text-tertiary);
}

.ctx-progress {
  display: block;
  height: 3px;
  margin-top: var(--sp-1);
  border-radius: var(--radius-pill);
  background: var(--muted-soft);
  overflow: hidden;
}

.ctx-progress i {
  display: block;
  height: 100%;
  border-radius: var(--radius-pill);
  background: var(--brand-gradient-line);
  transition: width var(--transition-base);
}

/* 快捷入口：胶囊描边按钮，与主 CTA（新建会话）拉开层级 */
.ctx-links button {
  padding: 3px var(--sp-2);
  font-family: inherit;
  font-size: 12px;
  line-height: 16px;
  color: var(--text-secondary);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  cursor: pointer;
  transition: color var(--transition-fast), border-color var(--transition-fast),
    background var(--transition-fast);
}

.ctx-links button:hover {
  color: var(--brand-hover);
  background: var(--brand-soft);
  border-color: var(--brand-line);
}

.ctx-links button:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
}

.ctx-links button.muted {
  color: var(--text-tertiary);
  background: transparent;
  border-color: transparent;
}

.ctx-links button.muted:hover {
  color: var(--text-secondary);
  background: var(--overlay-hover);
  border-color: var(--border-subtle);
}

.ctx-sub {
  margin-top: var(--sp-1);
  padding-top: var(--sp-2);
  border-top: 1px dashed var(--border-subtle);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.ctx-sub-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.ctx-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.ctx-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--text-tertiary);
  background: var(--neutral-soft);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
}

/* 有待审批时转成警示色，避免用户漏看 */
.ctx-count.has-items {
  color: var(--state-warning);
  background: var(--warning-soft);
  border-color: var(--warning-line);
}

/* 上下文栏内的空状态：去掉「卡中卡」的实底，动作降为次级按钮，
   把唯一的主按钮留给页头「新建会话」 */
.context-body :deep(.empty) {
  background: transparent;
}

.context-body :deep(.empty-icon) {
  color: var(--brand-primary);
}

.context-body :deep(.empty-action) {
  color: var(--text-primary);
  background: var(--bg-elevated);
  border-color: var(--border-strong);
}

.context-body :deep(.empty-action:hover) {
  color: var(--brand-hover);
  background: var(--overlay-hover);
  border-color: var(--brand-line);
}

.ctx-empty {
  margin: 0;
  padding: var(--sp-2) 0;
  font-size: 12px;
  color: var(--text-tertiary);
}

.link-actions {
  display: flex;
  gap: var(--sp-2);
}

@media (max-width: 1100px) {
  .context-panel {
    width: 280px;
  }
}
</style>
