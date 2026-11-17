<script setup lang="ts">
/**
 * 审核记录时间线。
 *
 * 折叠规则：按「审核对象 + 版本」把连续记录合并成一组（如「剧本草稿 · 审核 4 轮」），
 * 组间以竖线 + 节点表达时间序列（最新在上），默认只展开最新一组，其余点击展开历史。
 * 节点颜色使用语义令牌（成功 / 警告 / 失败 / 进行中），并配合文字标签，不单靠颜色表意。
 */
import { computed, ref, watch } from 'vue'
import type { ReviewCard } from '@/api/agent'
import StatusTag from '@/components/ui/StatusTag.vue'
import SkeletonList from '@/components/ui/SkeletonList.vue'
import { fmtTime } from './helpers'

const props = withDefaults(
  defineProps<{
    cards: ReviewCard[]
    loading?: boolean
  }>(),
  { loading: false },
)

type Tone = 'success' | 'warning' | 'danger' | 'running' | 'muted'
type TagKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'locked' | 'stale'

interface StatusMeta {
  label: string
  kind: TagKind
  tone: Tone
}

/** 审核状态 → 中文标签 + 语义令牌（颜色只作辅助通道） */
const STATUS_META: Record<string, StatusMeta> = {
  pending: { label: '待审核', kind: 'pending', tone: 'warning' },
  approved: { label: '已通过', kind: 'succeeded', tone: 'success' },
  rejected: { label: '已驳回', kind: 'failed', tone: 'danger' },
  revision_requested: { label: '修改中', kind: 'running', tone: 'running' },
  cancelled: { label: '已取消', kind: 'stale', tone: 'muted' },
  deferred: { label: '已暂缓', kind: 'locked', tone: 'muted' },
  expired: { label: '已过期', kind: 'stale', tone: 'muted' },
}

function metaOf(status: string): StatusMeta {
  return STATUS_META[status] ?? { label: status, kind: 'locked', tone: 'muted' }
}

/** 审核对象中文名称：剧本草稿 / 剧本第 2 版 */
function targetLabel(r: ReviewCard): string {
  const base = r.target_type === 'script' ? '剧本' : (r.target_type ?? '对象')
  const v = r.target_version
  if (!v) return `${base}草稿`
  return `${base}第 ${v} 版`
}

interface ReviewGroup {
  key: string
  label: string
  status: StatusMeta
  rounds: ReviewCard[]
  latestAt: string | null
}

/** 按版本倒序、同版本按轮次倒序，保证同类记录相邻（连续）便于合并 */
const sortedCards = computed(() =>
  [...props.cards].sort((a, b) => {
    const va = a.target_version ?? -1
    const vb = b.target_version ?? -1
    if (va !== vb) return vb - va
    return (b.round ?? 0) - (a.round ?? 0)
  }),
)

const groups = computed<ReviewGroup[]>(() => {
  const out: ReviewGroup[] = []
  for (const r of sortedCards.value) {
    const key = `${r.target_type}:${r.target_version ?? 0}`
    const last = out[out.length - 1]
    if (last && last.key === key) {
      last.rounds.push(r)
      continue
    }
    out.push({
      key,
      label: targetLabel(r),
      status: metaOf(r.status),
      rounds: [r],
      latestAt: r.created_at,
    })
  }
  // 组内按轮次升序（时间从上往下递进），最新一轮落在组尾
  for (const g of out) g.rounds.sort((a, b) => (a.round ?? 0) - (b.round ?? 0))
  return out
})

const totalRounds = computed(() => props.cards.length)

/** 展开的组：默认只展开最新一组 */
const expandedKeys = ref<string[]>([])

watch(
  groups,
  (list) => {
    expandedKeys.value = list.length ? [list[0].key] : []
  },
  { immediate: true },
)

function isOpen(key: string): boolean {
  return expandedKeys.value.includes(key)
}

function toggle(key: string): void {
  expandedKeys.value = isOpen(key)
    ? expandedKeys.value.filter((k) => k !== key)
    : [...expandedKeys.value, key]
}
</script>

<template>
  <section class="review-timeline" aria-label="审核记录">
    <div class="tl-caption">
      <span class="tl-caption-title">审核记录</span>
      <span class="tl-caption-meta">
        {{ groups.length }} 类 · {{ totalRounds }} 条
      </span>
    </div>

    <SkeletonList v-if="loading && !groups.length" variant="row" :rows="3" />

    <ol v-else class="tl-list">
      <li v-for="(g, gi) in groups" :key="g.key" class="tl-item" :class="`tone-${g.status.tone}`">
        <span class="tl-node" aria-hidden="true" />
        <div class="tl-body">
          <button
            type="button"
            class="tl-head"
            :aria-expanded="isOpen(g.key)"
            :aria-controls="`review-rounds-${gi}`"
            @click="toggle(g.key)"
          >
            <span class="tl-title">{{ g.label }} · 审核 {{ g.rounds.length }} 轮</span>
            <StatusTag :status="g.status.kind" :label="g.status.label" />
            <span class="tl-time">{{ fmtTime(g.latestAt) }}</span>
            <span class="tl-toggle">
              {{ isOpen(g.key) ? '收起历史' : `展开历史（${g.rounds.length}）` }}
            </span>
          </button>

          <ol v-show="isOpen(g.key)" :id="`review-rounds-${gi}`" class="round-list">
            <li
              v-for="(r, ri) in g.rounds"
              :key="r.confirmation_id"
              class="round-item"
              :class="{ 'is-latest': ri === g.rounds.length - 1 }"
            >
              <div class="round-head">
                <span class="round-no">{{ r.round != null ? `第 ${r.round} 轮` : '审核' }}</span>
                <StatusTag
                  :status="metaOf(r.status).kind"
                  :label="metaOf(r.status).label"
                />
                <span class="round-time">{{ fmtTime(r.created_at) }}</span>
                <span v-if="r.resolved_at" class="round-time">裁决于 {{ fmtTime(r.resolved_at) }}</span>
              </div>
              <p v-if="r.question" class="round-question">{{ r.question }}</p>
              <p v-if="r.user_response" class="round-response">用户回复：{{ r.user_response }}</p>
              <p v-if="r.comment" class="round-comment">备注：{{ r.comment }}</p>
            </li>
          </ol>
        </div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.review-timeline {
  flex-shrink: 0;
  max-height: 42vh;
  overflow-y: auto;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-sunken);
}

.tl-caption {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
}

.tl-caption-title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
  color: var(--text-primary);
}

.tl-caption-meta {
  font-size: 11px;
  color: var(--text-tertiary);
}

/* 时间线：竖线由列表本身绘制，节点绝对定位在线上 */
.tl-list {
  position: relative;
  margin: 0;
  padding: 0 0 0 var(--sp-4);
  list-style: none;
}

.tl-list::before {
  content: '';
  position: absolute;
  left: 4px;
  top: var(--sp-2);
  bottom: var(--sp-3);
  width: 1px;
  background: var(--border-subtle);
}

.tl-item {
  position: relative;
  padding-bottom: var(--sp-3);
}

.tl-item:last-child {
  padding-bottom: 0;
}

.tl-node {
  position: absolute;
  left: calc(-1 * var(--sp-4) + 1px);
  top: 7px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-tertiary);
  box-shadow: 0 0 0 3px var(--bg-sunken);
}

.tl-item.tone-success .tl-node {
  background: var(--state-success);
  box-shadow: 0 0 0 3px var(--bg-sunken), 0 0 0 4px var(--success-line);
}

.tl-item.tone-warning .tl-node {
  background: var(--state-warning);
  box-shadow: 0 0 0 3px var(--bg-sunken), 0 0 0 4px var(--warning-line);
}

.tl-item.tone-danger .tl-node {
  background: var(--state-danger);
  box-shadow: 0 0 0 3px var(--bg-sunken), 0 0 0 4px var(--danger-line);
}

.tl-item.tone-running .tl-node {
  background: var(--brand-primary);
  box-shadow: 0 0 0 3px var(--bg-sunken), 0 0 0 4px var(--brand-line);
}

.tl-item.tone-muted .tl-node {
  background: var(--text-tertiary);
  box-shadow: 0 0 0 3px var(--bg-sunken), 0 0 0 4px var(--muted-line);
}

.tl-body {
  min-width: 0;
}

.tl-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-1) var(--sp-2);
  margin-left: calc(-1 * var(--sp-2));
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  color: var(--text-primary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius);
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}

.tl-head:hover {
  background: var(--overlay-hover);
  border-color: var(--border-subtle);
}

.tl-head:focus-visible {
  outline: 2px solid var(--brand-hover);
  outline-offset: 1px;
}

.tl-title {
  font-weight: 600;
  color: var(--text-primary);
}

.tl-time {
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.tl-toggle {
  margin-left: auto;
  font-size: 11px;
  color: var(--brand-primary);
}

.round-list {
  margin: var(--sp-1) 0 0;
  padding: 0 0 0 var(--sp-2);
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  border-left: 1px solid var(--border-subtle);
}

.round-item {
  padding: var(--sp-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-panel);
}

.round-item.is-latest {
  border-color: var(--border-strong);
}

.round-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.round-no {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.round-time {
  font-size: 11px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.round-question {
  margin: var(--sp-1) 0 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-secondary);
}

.round-response {
  margin: var(--sp-1) 0 0;
  padding: var(--sp-1) var(--sp-2);
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-primary);
  background: var(--brand-soft);
  border-radius: var(--radius-sm);
}

.round-comment {
  margin: var(--sp-1) 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-tertiary);
}
</style>
