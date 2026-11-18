<script setup lang="ts">
/** 作品卡片：封面 + 状态 + 标题 + 元信息 + 进度。整卡可点，右上角「⋯」走操作菜单。 */
import { computed } from 'vue'
import { NButton, NDropdown, NProgress, NTag } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import type { CreationType, Project } from '@/types/project'
import { stageLabel } from '@/constants/stages'

const props = defineProps<{
  project: Project
  /** 列表序号，用于错峰入场动画 */
  index: number
}>()
const emit = defineEmits<{
  open: [id: string]
  action: [key: string, project: Project]
}>()

const STATUS_META: Record<Project['status'], { label: string; type: 'default' | 'success' | 'warning' }> = {
  draft: { label: '草稿', type: 'warning' },
  active: { label: '进行中', type: 'success' },
  archived: { label: '已归档', type: 'default' },
}
const CREATION_LABEL: Record<CreationType, string> = {
  novel: '小说改编',
  script: '剧本直出',
}

const statusMeta = computed(() => STATUS_META[props.project.status])
const creationLabel = computed(() => CREATION_LABEL[props.project.creation_type])
const coverChar = computed(() => {
  const ch = props.project.name.trim().charAt(0)
  return ch ? ch.toUpperCase() : 'P'
})
const ratioClass = computed(() => `cover-${props.project.aspect_ratio.replace(':', '-')}`)
const animationDelay = computed(() => `${Math.min(props.index, 12) * 0.04}s`)
const updatedTitle = computed(() => new Date(props.project.updated_at).toLocaleString('zh-CN'))
const updatedText = computed(() => relativeTime(props.project.updated_at))

/**
 * 进度 = 已完成镜头 / 总镜头（后端派生字段 shot_done / shot_total）。
 * 此前读 episode_plan[].shot_count，该字段后端从不返回，导致进度恒为 0%。
 */
const shotTotal = computed(() => props.project.shot_total ?? 0)
const shotDone = computed(() => props.project.shot_done ?? 0)
const hasShots = computed(() => shotTotal.value > 0)
const percent = computed(() =>
  hasShots.value ? Math.round((shotDone.value / shotTotal.value) * 100) : 0,
)
/** 当前真实生产阶段（后端 current_stage 派生），未知枚举原样显示 */
const stageText = computed(() => stageLabel(props.project.current_stage))

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

const menuOptions = computed<DropdownOption[]>(() => [
  { key: 'copy', label: '复制（后续版本支持）', disabled: true },
  { key: 'archive', label: '归档', disabled: props.project.status === 'archived' },
  { key: 'delete', label: '删除（仅从列表移除）', props: { style: 'color: var(--danger);' } },
  { key: 'purge', label: '彻底删除（含媒体文件）', props: { style: 'color: var(--danger);' } },
])

function onCardKey(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    emit('open', props.project.id)
  }
}
</script>

<template>
  <article
    class="project-card"
    role="button"
    tabindex="0"
    :aria-label="`打开作品 ${project.name}`"
    :style="{ animationDelay }"
    @click="emit('open', project.id)"
    @keydown="onCardKey"
  >
    <div class="cover" :class="ratioClass">
      <span class="cover-char" aria-hidden="true">{{ coverChar }}</span>
      <n-tag class="status-tag" :type="statusMeta.type" size="small" round>
        {{ statusMeta.label }}
      </n-tag>
      <span class="cover-ratio">{{ project.aspect_ratio }}</span>
    </div>
    <div class="card-body">
      <div class="card-title-row">
        <h3 class="card-title" :title="project.name">{{ project.name }}</h3>
        <n-dropdown
          trigger="click"
          :options="menuOptions"
          @select="emit('action', String($event), project)"
          @click.stop
        >
          <n-button quaternary size="tiny" class="more-btn" aria-label="更多操作" @click.stop>⋯</n-button>
        </n-dropdown>
      </div>
      <div class="card-meta">
        <span>{{ creationLabel }}</span>
        <span :title="updatedTitle">更新于 {{ updatedText }}</span>
      </div>
      <div class="progress-row">
        <!-- 当前真实生产阶段（后端 current_stage），不根据页面访问记录推断 -->
        <span class="stage-chip" :title="`当前阶段：${stageText}`">{{ stageText }}</span>
        <template v-if="hasShots">
          <n-progress
            class="progress-bar"
            type="line"
            :percentage="percent"
            :height="6"
            :border-radius="3"
            :show-indicator="false"
            color="var(--brand-primary)"
            rail-color="var(--border-subtle)"
            :aria-label="`镜头完成 ${shotDone} / ${shotTotal}`"
          />
          <span class="progress-text">{{ shotDone }}/{{ shotTotal }} 镜</span>
        </template>
      </div>
    </div>
  </article>
</template>

<style scoped>
.project-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  overflow: hidden;
  cursor: pointer;
  box-shadow: var(--shadow-lg);
  transition:
    box-shadow 0.2s ease,
    border-color 0.2s ease,
    transform 0.2s ease;
  animation: card-fade-up 0.35s ease both;
}
@keyframes card-fade-up {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.project-card:hover {
  border-color: var(--brand-primary);
  box-shadow: 0 2px 8px var(--brand-soft);
  transform: translateY(-2px);
}
.project-card:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
  border-color: var(--brand-primary);
}
@media (prefers-reduced-motion: reduce) {
  .project-card {
    animation: none;
    transition: none;
  }
  .project-card:hover {
    transform: none;
  }
}
.cover {
  position: relative;
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
}
.cover-char {
  font-size: 44px;
  font-weight: 700;
  color: var(--overlay-active);
  text-shadow: 0 2px 8px var(--overlay-active);
  user-select: none;
}
.cover-9-16 {
  background: var(--overlay-selected);
}
.cover-16-9 {
  background: var(--bg-panel);
}
.cover-1-1 {
  background: var(--bg-elevated);
}
.status-tag {
  position: absolute;
  top: 12px;
  left: 12px;
}
.cover-ratio {
  position: absolute;
  right: 12px;
  bottom: 8px;
  font-size: 12px;
  line-height: 18px;
  color: var(--overlay-active);
  letter-spacing: 0.5px;
}
.card-body {
  padding: var(--sp-3);
}
.card-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.card-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-size: 17px;
  line-height: 26px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.more-btn {
  color: var(--text-secondary);
}
.card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-top: var(--sp-1);
  font-size: 13px;
  line-height: 20px;
  color: var(--text-secondary);
}
.progress-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}
.stage-chip {
  flex: none;
  padding: 1px 8px;
  border-radius: var(--radius-sm);
  background: var(--brand-soft);
  color: var(--brand-primary);
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;
}
.progress-bar {
  flex: 1;
}
.progress-text {
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
  min-width: 56px;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
</style>
