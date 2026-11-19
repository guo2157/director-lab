<script setup lang="ts">
/** 作品卡片：封面 + 状态 + 标题 + 元信息 + 进度。整卡可点，右上角「⋯」走操作菜单。 */
import { computed } from 'vue'
import { NButton, NDropdown, NProgress, NTag } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import type { CreationType, Project } from '@/types/project'
import { stageLabel } from '@/constants/stages'
import { relativeTime } from '@/utils/time'

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

/* 状态徽标只反映后端 project.status（与筛选计数同源，不允许前端另立口径）；
   「是否完成」由进度区的 is-done 表达，两个信息不混在同一个徽标里。 */
const statusMeta = computed(() => STATUS_META[props.project.status])
const creationLabel = computed(() => CREATION_LABEL[props.project.creation_type])
const coverChar = computed(() => {
  const ch = props.project.name.trim().charAt(0)
  return ch ? ch.toUpperCase() : 'P'
})
const ratioClass = computed(() => `cover-${props.project.aspect_ratio.replace(':', '-')}`)
/** 画幅角标带方向词，避免「9:16」被误读成时长 */
const ratioLabel = computed(() => {
  const map: Record<string, string> = { '9:16': '竖屏', '16:9': '横屏', '1:1': '方形' }
  const dir = map[props.project.aspect_ratio]
  return dir ? `${props.project.aspect_ratio} ${dir}` : props.project.aspect_ratio
})
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
/** 进度语义：无镜头 = 未开始；有镜头但 0 完成 = 待启动；100% = 已完成 */
const progressState = computed<'empty' | 'zero' | 'partial' | 'done'>(() => {
  if (!hasShots.value) return 'empty'
  if (percent.value >= 100) return 'done'
  if (percent.value === 0) return 'zero'
  return 'partial'
})
/** 当前真实生产阶段（后端 current_stage 派生），未知枚举原样显示 */
const stageText = computed(() => stageLabel(props.project.current_stage))

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
      <span class="cover-ratio">{{ ratioLabel }}</span>
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
      <!-- 进度区固定两行：上行阶段 + 计数，下行通栏进度条，保证跨卡纵向可比 -->
      <div class="progress-block" :class="`is-${progressState}`">
        <div class="progress-head">
          <span class="stage-chip" :title="`当前阶段：${stageText}`">{{ stageText }}</span>
          <span class="progress-text">
            <template v-if="hasShots">{{ shotDone }}/{{ shotTotal }} 镜</template>
            <template v-else>未开始</template>
          </span>
        </div>
        <div v-if="hasShots" class="progress-track">
          <n-progress
            class="progress-bar"
            type="line"
            :percentage="percent"
            :height="4"
            :border-radius="2"
            :show-indicator="false"
            :color="progressState === 'done' ? 'var(--state-success)' : 'var(--brand-primary)'"
            :rail-color="progressState === 'zero' ? 'var(--muted-line)' : 'var(--border-subtle)'"
            :aria-label="`镜头完成 ${shotDone} / ${shotTotal}`"
          />
          <span
            class="progress-done"
            :class="{ 'is-placeholder': progressState !== 'done' }"
            aria-hidden="true"
          >{{ progressState === 'done' ? '✓' : '' }}</span>
        </div>
        <div v-else class="progress-track is-empty" aria-hidden="true" />
      </div>
    </div>
  </article>
</template>

<style scoped>
.project-card {
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  overflow: hidden;
  cursor: pointer;
  /* 深色界面用边框与背景明度表达层级，不叠投影（DESIGN.md §1.1） */
  box-shadow: none;
  transition:
    border-color var(--transition-fast),
    background-color var(--transition-fast);
  animation: card-fade-up 0.35s ease both;
}
@keyframes card-fade-up {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.project-card:hover {
  border-color: var(--border-strong);
  background: var(--bg-elevated);
}
.project-card:hover .cover {
  border-bottom-color: var(--border-strong);
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
}
.cover {
  position: relative;
  /* 封面只做身份标识，不占半张卡：固定 104px，信息密度让给标题与进度 */
  height: 104px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-sunken);
}
.cover-char {
  font-family: var(--font-display);
  font-size: 40px;
  font-weight: 600;
  line-height: 1;
  /* 水印式首字：可见但不抢标题，比原 0.08 透明度提升到可辨识档 */
  color: var(--border-strong);
  user-select: none;
}
.cover-9-16 {
  background: linear-gradient(180deg, var(--brand-soft) 0%, var(--bg-sunken) 100%);
}
.cover-16-9 {
  background: var(--bg-sunken);
}
.cover-1-1 {
  background: var(--bg-elevated);
}
.status-tag {
  position: absolute;
  top: 10px;
  left: 10px;
}
.cover-ratio {
  position: absolute;
  right: 8px;
  bottom: 8px;
  padding: 0 6px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 17px;
  border-radius: var(--radius-sm);
  /* 角标压在封面图上，需要自身底衬保证可读（--bg-sunken 上 --text-secondary 已过 4.5:1） */
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  letter-spacing: 0.3px;
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
  font-size: 16px;
  line-height: 24px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.more-btn {
  color: var(--text-tertiary);
  flex: none;
  /* 触摸目标不小于 28px（视觉仍是 tiny 图标按钮） */
  min-width: 28px;
  min-height: 28px;
}
.more-btn:hover {
  color: var(--text-primary);
}
.card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-top: 2px;
  font-size: 12.5px;
  line-height: 19px;
  color: var(--text-tertiary);
}
/* 进度区：上行阶段+计数，下行通栏进度条 —— 行结构与边距固定，跨卡对齐。
   min-height 预留轨道行高，使「无镜头」的卡与有进度的卡等高。 */
.progress-block {
  margin-top: var(--sp-3);
  padding-top: var(--sp-2);
  border-top: 1px solid var(--border-subtle);
  min-height: 48px;
}
.progress-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
/* 阶段 chip 用中性色：品牌青只留给可操作元素与进度填充，
   避免「阶段名 / 状态 / CTA」三种语义挤在同一色相上 */
.stage-chip {
  flex: none;
  padding: 1px 7px;
  border-radius: var(--radius-sm);
  background: var(--neutral-soft);
  color: var(--text-secondary);
  font-size: 11.5px;
  line-height: 18px;
  white-space: nowrap;
}
.progress-text {
  font-size: 12px;
  line-height: 18px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.progress-track {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  /* 固定高度：有无 ✓ 图标都不改变行高，保证四张卡进度条基线一致 */
  height: 12px;
  margin-top: 5px;
}
.progress-bar {
  flex: 1;
  min-width: 0;
}
.progress-done {
  flex: none;
  font-size: 12px;
  line-height: 1;
  color: var(--state-success);
}
/* 无镜头（未规划分镜）：轨道隐藏，进度区只保留阶段名 + 「未开始」，
   避免出现一条和「0% 已规划」难以区分的空轨道。 */
.progress-track.is-empty {
  display: none;
}
.progress-block.is-empty .progress-text {
  color: var(--text-tertiary);
}
/* 有镜头但 0%：轨道由 rail-color 提到 --muted-line，避免看起来像一条分隔线 */
.progress-block.is-zero .progress-text {
  color: var(--text-tertiary);
}
/* 已完成：只让进度条（绿 + ✓）表达完成，阶段 chip 保持中性。
   避免「进行中」徽标与「已完成」阶段名同时染绿造成语义打架。 */
.progress-block.is-done .progress-text {
  color: var(--state-success);
}
/* ✓ 占据固定 14px 槽位：所有卡的进度条右端都在同一 x，满格卡不因勾选而缩短 */
.progress-done {
  flex: none;
  width: 14px;
  text-align: center;
}
.progress-done.is-placeholder {
  visibility: hidden;
}
</style>
