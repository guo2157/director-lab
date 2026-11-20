<script setup lang="ts">
/** 作品卡片：封面 + 状态 + 标题 + 元信息 + 进度。整卡可点，右上角「⋯」走操作菜单。 */
import { computed, ref, watch } from 'vue'
import { NButton, NDropdown, NProgress } from 'naive-ui'
import type { DropdownOption } from 'naive-ui'
import type { CreationType, Project } from '@/types/project'
import { stageLabel } from '@/constants/stages'
import { relativeTime } from '@/utils/time'

const props = defineProps<{
  project: Project
  /** 列表序号，用于错峰入场动画 */
  index: number
  /** 真实封面地址（来自项目已有资产）；空串表示该项目还没有任何可用图片 */
  coverUrl?: string
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
/** 封面不可用（加载失败 / 画面近乎纯色）时回退占位，避免裂图或「黑块」 */
const coverFailed = ref(false)
/** 真正展示图片：有地址且未被判定为不可用 */
const showImage = computed(() => Boolean(props.coverUrl) && !coverFailed.value)
watch(
  () => props.coverUrl,
  () => {
    coverFailed.value = false
  },
)
/**
 * 封面加载完成后的可用性判定。
 *
 * 为什么需要：库里存在 `[MOCK] job xxx out cover` 这类测试图，
 * 尺寸合规（400×225）、接口 200、DB 无可判别字段，
 * 但画面只是一块深色 + 一行小字 + 一圈黄边，铺在卡片上像坏了。
 *
 * 实测（24×24 降采样、每通道量化到 5bit 后的不同颜色数）：
 *   · mock 占位图      19 种   ← 需排除
 *   · 暗色星云分镜图   58 种   ← 真实内容，需保留
 *   · 人物夜景分镜图  230 种
 * 阈值取 28：既排除 mock，又给「低细节但真实」的图留出余量。
 *
 * 判定失败（canvas 被污染 / 解码异常）时一律按可用处理 ——
 * 宁可显示一张普通图，也不能因为检测本身出错把真实封面隐藏掉。
 */
const MIN_DISTINCT_COLORS = 28
function onCoverLoad(e: Event) {
  const img = e.target as HTMLImageElement
  try {
    const S = 24
    const canvas = document.createElement('canvas')
    canvas.width = S
    canvas.height = S
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    if (!ctx) return
    ctx.drawImage(img, 0, 0, S, S)
    const { data } = ctx.getImageData(0, 0, S, S)
    // 量化到 5bit/通道后再统计，避免照片噪点把颜色数虚高
    const seen = new Set<number>()
    for (let i = 0; i < data.length; i += 4) {
      const key = ((data[i] >> 3) << 10) | ((data[i + 1] >> 3) << 5) | (data[i + 2] >> 3)
      seen.add(key)
    }
    if (seen.size < MIN_DISTINCT_COLORS) coverFailed.value = true
  } catch {
    // canvas 被污染或解码失败：不做判定，保留图片
  }
}
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
    <div class="cover" :class="[ratioClass, { 'has-image': showImage }]">
      <!-- 有真实资产图时直接铺满：作品列表的第一落点应该是画面本身。
           无图 / 加载失败 / 画面近乎纯色（mock 占位图）时退回占位，不伪造图片。 -->
      <img
        v-if="coverUrl"
        v-show="showImage"
        class="cover-img"
        :src="coverUrl"
        :alt="`${project.name} 封面`"
        loading="lazy"
        decoding="async"
        @load="onCoverLoad"
        @error="coverFailed = true"
      />
      <template v-if="!showImage">
        <span class="cover-orb" aria-hidden="true" />
        <span class="cover-char" aria-hidden="true">{{ coverChar }}</span>
      </template>

      <span class="status-badge" :class="`is-${project.status}`">{{ statusLabel }}</span>
      <!-- 画幅角标：左侧画一个真实的画幅形状（竖条/横条/方框），
           文字只是补充。只用「9:16 竖屏」五个字而封面是横条，语义是矛盾的。 -->
      <span class="cover-ratio">
        <span class="ratio-frame" :class="ratioClass" aria-hidden="true" />
        {{ ratioLabel }}
      </span>
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
            :rail-color="progressState === 'zero' ? 'var(--border-strong)' : 'var(--border-subtle)'"
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
  /* 渐变描边：顶部亮、底部暗，模拟环境光从左上方打下来。
     纯 1px 实色边框在 --bg-base 上明度差太小，卡面会读成「贴在黑板上的色块」。 */
  border: 1px solid transparent;
  border-radius: var(--radius-xl);
  /* 卡面用 --bg-elevated（而非 --bg-panel）：与 --bg-base 的明度差从 1.05:1 提到 1.28:1，
     否则卡片"面"在近黑底上完全看不见，只剩一圈描边在撑结构。
     面内再叠一层自上而下的极淡渐变，给卡面一点材质而不是一块平色。 */
  background:
    linear-gradient(
        180deg,
        var(--overlay-hover) 0%,
        transparent 38%,
        transparent 100%
      )
      padding-box,
    linear-gradient(var(--bg-elevated), var(--bg-elevated)) padding-box,
    linear-gradient(180deg, var(--border-strong) 0%, var(--border-subtle) 42%, var(--border-subtle) 100%)
      border-box;
  box-shadow: var(--card-sheen);
  overflow: hidden;
  cursor: pointer;
  transition:
    background var(--transition-fast),
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
/* 悬停：描边转品牌渐变 + 外发光 + 抬起 2px */
.project-card:hover {
  background:
    linear-gradient(180deg, var(--overlay-active) 0%, transparent 42%) padding-box,
    linear-gradient(var(--bg-elevated), var(--bg-elevated)) padding-box,
    linear-gradient(180deg, var(--brand-line) 0%, var(--border-strong) 100%) border-box;
  box-shadow: var(--card-sheen-strong), var(--shadow-glow);
  transform: translateY(-2px);
}
.project-card:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 2px;
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

/* 封面：占位态与有图态必须同高，否则同一行卡片高矮不齐
   （实测占位 116px vs 有图 251px，第 1 张卡被拉伸到 381px 且内容区多出空白）。

   比例必须跟随项目画幅，不能一律 4:3：
   本产品做的是短剧 / 漫剧，项目绝大多数是 9:16 竖屏（实测库内 4/4 全是 9:16），
   而 4:3 横版容器会把竖图从上下各裁掉约 44% —— 卡片左上角写着「9:16 竖屏」、
   封面却是横条，这是语义矛盾，也是首屏最主要的画面损失。
   基准改为 3:4（竖版海报比例）：竖图只做轻微裁切，横图 / 方图各有专属分支。
   3:4 而不是原生的 9:16 是因为 9:16 在 336px 列宽下会得到 597px 高的封面，
   单卡超过 730px，一屏放不下一行，列表失去可比性。 */
.cover {
  position: relative;
  aspect-ratio: 3 / 4;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-bottom: 1px solid var(--border-subtle);
  background: linear-gradient(160deg, var(--bg-elevated) 0%, var(--bg-sunken) 100%);
}
/* 按项目画幅覆盖容器比例；占位态与有图态共用同一套，避免混排时高矮不齐 */
.cover.cover-16-9 {
  aspect-ratio: 16 / 9;
}
.cover.cover-1-1 {
  aspect-ratio: 1 / 1;
}

/* 网格底纹：24px 细线，给「空封面」一点材质，而不是一块死黑。
   有真实封面图时不需要，直接隐藏（否则会和图片纹理打架）。 */
.cover::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(to right, var(--neutral-line) 1px, transparent 1px),
    linear-gradient(to bottom, var(--neutral-line) 1px, transparent 1px);
  background-size: 24px 24px;
  opacity: 0.5;
  pointer-events: none;
}
.cover.has-image::before {
  display: none;
}

/* 真实封面图：铺满 + 轻微上暗下亮渐变，保证压在图上角标始终可读 */
.cover-img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  /* 竖屏源图裁成横向容器时，默认居中会把头裁掉；偏上取景保住面部 */
  object-position: center 22%;
  display: block;
  transition: transform var(--transition-base);
}
.project-card:hover .cover-img {
  transform: scale(1.03);
}
/* 角标可读性底衬：图上的文字必须有局部暗场，否则浅色画面会把字吃掉 */
.cover.has-image::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    180deg,
    var(--scrim) 0%,
    transparent 34%,
    transparent 68%,
    var(--scrim) 100%
  );
  opacity: 0.72;
  pointer-events: none;
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

/* 首字水印：降到 --border-strong 档（水印语义），不再靠它当唯一视觉焦点 */
.cover-char {
  position: relative;
  font-family: var(--font-display);
  font-size: 38px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -0.02em;
  color: var(--border-strong);
  text-shadow: 0 0 20px var(--brand-glow);
  user-select: none;
  transition: color var(--transition-base);
}
.project-card:hover .cover-char {
  color: var(--text-tertiary);
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

/* 状态徽标：胶囊 + 圆点，只翻译后端 status（与筛选计数同源）。
   放右上角，把左上角让给画幅角标 —— 两者都压在同一侧会互相削弱。 */
.status-badge {
  position: absolute;
  top: 10px;
  right: 10px;
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
  backdrop-filter: blur(4px);
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
  left: 10px;
  top: 10px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 8px 0 6px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 18px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  letter-spacing: 0.2px;
  backdrop-filter: blur(4px);
}
/* 画幅形状：用真实比例的小矩形表达 9:16 / 16:9 / 1:1，
   比「9:16 竖屏」五个字更直接（横向封面 + 竖屏文字曾是语义矛盾）。 */
.ratio-frame {
  flex: none;
  border: 1px solid currentColor;
  border-radius: 1px;
  opacity: 0.75;
}
.ratio-frame.cover-9-16 {
  width: 6px;
  height: 10px;
  background: transparent;
}
.ratio-frame.cover-16-9 {
  width: 11px;
  height: 6px;
  background: transparent;
}
.ratio-frame.cover-1-1 {
  width: 8px;
  height: 8px;
  background: transparent;
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
  color: var(--text-secondary);
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
/* 有镜头但 0%：轨道由 rail-color 提到 --border-strong，避免在近黑卡面上完全消失
   （原 --muted-line 在 --bg-panel 上实测几乎不可见，扫视时区分不出「未开始」与「0%」） */
.progress-block.is-zero .progress-text {
  color: var(--text-tertiary);
}
/* 已完成：只让进度条（绿 + ✓）表达完成，阶段 chip 保持中性 */
.progress-block.is-done .progress-text {
  color: var(--state-success);
}

/* 窄屏双列（390 视口卡宽约 165px）：内边距与标题档位必须同时收，
   否则 16px 内边距 + 15.5px 标题会把可读宽度压到 133px，
   标题只能显示 6–7 个字，封面占比也被挤小。 */
@media (max-width: 480px) {
  .card-body {
    padding: var(--sp-2) var(--sp-3);
  }
  .card-title {
    font-size: 13.5px;
    line-height: 19px;
  }
  .card-meta {
    font-size: 11px;
  }
  .stage-chip {
    font-size: 11px;
  }
  .progress-block {
    padding-top: var(--sp-2);
    min-height: 44px;
  }
  /* 双列下「⋯」与标题争宽，且卡片本身可点开详情，更多操作在窄屏不是高频入口；
     收成 24px 触摸目标并去掉负边距，避免压到标题基线 */
  .more-btn {
    min-width: 24px;
    min-height: 24px;
    margin-top: -1px;
  }
}
</style>
