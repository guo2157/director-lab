<script setup lang="ts">
/** 作品卡片：封面 + 状态 + 标题 + 元信息 + 进度。整卡可点，右上角「⋯」走操作菜单。 */
import { computed } from 'vue'
import { NButton, NDropdown, NProgress } from 'naive-ui'
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

const STATUS_LABEL: Record<Project['status'], string> = {
  draft: '草稿',
  active: '进行中',
  archived: '已归档',
}
const CREATION_LABEL: Record<CreationType, string> = {
  novel: '小说改编',
  script: '剧本直出',
}

/* 状态徽标只反映后端 project.status（与筛选计数同源，不允许前端另立口径）；
   「是否完成」由进度区的 is-done 表达，两个信息不混在同一个徽标里。 */
const statusLabel = computed(() => STATUS_LABEL[props.project.status])
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
const animationDelay = computed(() => `${Math.min(props.index, 12) * 0.045}s`)
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
  { key: 'delete', label: '删除（仅从列表移除）', props: { style: 'color: var(--danger-text);' } },
  { key: 'purge', label: '彻底删除（含媒体文件）', props: { style: 'color: var(--danger-text);' } },
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
      <!-- 封面：画幅暗示的发光核 + 首字水印，两层都只是身份标识，不承载信息 -->
      <span class="cover-orb" aria-hidden="true" />
      <span class="cover-char" aria-hidden="true">{{ coverChar }}</span>

      <span class="status-badge" :class="`is-${project.status}`">{{ statusLabel }}</span>
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
        <span class="meta-kind">{{ creationLabel }}</span>
        <span class="meta-dot" aria-hidden="true">·</span>
        <span :title="updatedTitle">更新于 {{ updatedText }}</span>
      </div>

      <!-- 进度区固定两行：上行阶段 + 计数，下行通栏进度条，保证跨卡纵向可比 -->
      <div class="progress-block" :class="`is-${progressState}`">
        <div class="progress-head">
          <span class="stage-chip" :title="`当前阶段：${stageText}`">{{ stageText || '未开始' }}</span>
          <span class="progress-text">
            <template v-if="hasShots">{{ shotDone }}/{{ shotTotal }} 镜</template>
            <template v-else>未规划</template>
          </span>
        </div>
        <div v-if="hasShots" class="progress-track">
          <n-progress
            class="progress-bar"
            type="line"
            :percentage="percent"
            :height="5"
            :border-radius="3"
            :show-indicator="false"
            :color="progressState === 'done' ? 'var(--state-success)' : 'var(--brand-primary)'"
            :rail-color="progressState === 'zero' ? 'var(--neutral-line)' : 'var(--border-subtle)'"
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
  position: relative;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-xl);
  box-shadow: var(--card-sheen);
  overflow: hidden;
  cursor: pointer;
  transition:
    border-color var(--transition-fast),
    background-color var(--transition-fast),
    transform var(--transition-fast),
    box-shadow var(--transition-fast);
  animation: card-fade-up 0.4s var(--ease-out) both;
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
  border-color: var(--brand-line);
  background: var(--bg-elevated);
  box-shadow: var(--card-sheen-strong), var(--shadow-glow);
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

/* 封面：固定 116px。深色渐变 + 中心发光核，代替一张空占位图 */
.cover {
  position: relative;
  height: 116px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-bottom: 1px solid var(--border-subtle);
  background: linear-gradient(160deg, var(--bg-elevated) 0%, var(--bg-sunken) 100%);
}

/* 发光核：径向渐变，只用令牌变量拼，不引入新色值 */
.cover-orb {
  position: absolute;
  width: 190px;
  height: 190px;
  border-radius: 50%;
  background: radial-gradient(
    circle at 50% 46%,
    var(--brand-line) 0%,
    var(--brand-soft) 34%,
    transparent 70%
  );
  transition: background var(--transition-base);
}
.project-card:hover .cover-orb {
  background: radial-gradient(
    circle at 50% 46%,
    var(--brand-hover) 0%,
    var(--brand-line) 30%,
    transparent 72%
  );
}

.cover-char {
  position: relative;
  font-family: var(--font-display);
  font-size: 42px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.02em;
  color: var(--text-tertiary);
  text-shadow: 0 0 24px var(--brand-glow);
  user-select: none;
  transition: color var(--transition-base);
}
.project-card:hover .cover-char {
  color: var(--text-primary);
}

/* 画幅差异：竖屏更「冷」，横屏偏中性，方形偏暖 —— 靠渐变角度与底色档位区分 */
.cover-9-16 {
  background: linear-gradient(165deg, var(--bg-elevated) 0%, var(--bg-sunken) 100%);
}
.cover-16-9 {
  background: linear-gradient(180deg, var(--bg-panel) 0%, var(--bg-sunken) 100%);
}
.cover-1-1 {
  background: linear-gradient(135deg, var(--bg-elevated) 0%, var(--bg-sunken) 100%);
}

/* 状态徽标：胶囊 + 圆点，只翻译后端 status（与筛选计数同源） */
.status-badge {
  position: absolute;
  top: 10px;
  left: 10px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px;
  font-size: 11.5px;
  line-height: 17px;
  font-weight: 500;
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
  white-space: nowrap;
}
.status-badge::before {
  content: '';
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}
.status-badge.is-draft {
  color: var(--state-warning);
  background: var(--warning-soft);
  border-color: var(--warning-line);
}
.status-badge.is-active {
  color: var(--brand-hover);
  background: var(--brand-soft);
  border-color: var(--brand-line);
}
.status-badge.is-archived {
  color: var(--text-tertiary);
  background: var(--muted-soft);
  border-color: var(--muted-line);
}

.cover-ratio {
  position: absolute;
  right: 10px;
  bottom: 10px;
  padding: 0 7px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 18px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  letter-spacing: 0.2px;
}

.card-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  padding: var(--sp-4);
}

.card-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-2);
}
.card-title {
  flex: 1;
  min-width: 0;
  margin: 0;
  font-family: var(--font-display);
  font-size: 15.5px;
  line-height: 22px;
  font-weight: 600;
  letter-spacing: -0.008em;
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
  margin-top: -3px;
}
.more-btn:hover {
  color: var(--text-primary);
}

.card-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 3px;
  font-size: 12px;
  line-height: 18px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
}
.meta-dot {
  color: var(--border-strong);
}

/* 进度区：上行阶段+计数，下行通栏进度条 —— 行结构固定，跨卡对齐。
   min-height 预留轨道行高，使「未规划」的卡与有进度的卡等高。 */
.progress-block {
  margin-top: auto;
  padding-top: var(--sp-3);
  min-height: 50px;
}
.progress-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
/* 阶段 chip 用中性色：品牌青只留给可操作元素与进度填充 */
.stage-chip {
  flex: none;
  padding: 1px 8px;
  border-radius: var(--radius-pill);
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
  margin-top: 6px;
}
.progress-bar {
  flex: 1;
  min-width: 0;
}
/* ✓ 占据固定 14px 槽位：所有卡的进度条右端都在同一 x，满格卡不因勾选而缩短 */
.progress-done {
  flex: none;
  width: 14px;
  text-align: center;
  font-size: 12px;
  line-height: 1;
  color: var(--state-success);
}
.progress-done.is-placeholder {
  visibility: hidden;
}
/* 无镜头（未规划分镜）：轨道隐藏，进度区只保留阶段名 + 「未规划」，
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
/* 已完成：只让进度条（绿 + ✓）表达完成，阶段 chip 保持中性 */
.progress-block.is-done .progress-text {
  color: var(--state-success);
}
</style>
