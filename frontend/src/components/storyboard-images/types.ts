import type { Shot } from '@/types/script'

/** 与 global.css 的 .status-tag.is-* 一一对应 */
export type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'

export interface ShotStatusView {
  kind: StatusKind
  label: string
}

/**
 * 镜头状态判定：在途 > 失败 > 未生成 > 已生成。
 * 卡片角标与详情页共用同一套判定，避免两处说法不一致。
 *
 * 逐镜人工确认（keyframe_confirmed）已取消：状态只反映「产物是否存在」，
 * 不再出现「待确认 / 已确认」这类阶段性审核语义。
 */
export function shotStatus(shot: Shot, opts: { inflightText?: string; failed?: boolean } = {}): ShotStatusView {
  const hasKeyframe = Boolean(shot.current_assets?.keyframe)
  if (opts.inflightText) return { kind: 'generating', label: opts.inflightText }
  if (opts.failed) {
    return hasKeyframe
      ? { kind: 'failed', label: '重新生成失败' }
      : { kind: 'failed', label: '失败 · 待重做' }
  }
  if (!hasKeyframe) return { kind: 'stale', label: '待生成' }
  return { kind: 'succeeded', label: '已生成' }
}

/** 状态筛选：全部 / 已生成 / 待生成 / 失败（按产物存在与否，不含审核态） */
export type ShotFilter = 'all' | 'done' | 'pending' | 'failed'

export const SHOT_FILTERS: Array<{ value: ShotFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'done', label: '已生成' },
  { value: 'pending', label: '待生成' },
  { value: 'failed', label: '失败' },
]

/** 镜头在某个筛选下是否可见 */
export function matchFilter(
  filter: ShotFilter,
  shot: Shot,
  opts: { failed?: boolean } = {},
): boolean {
  if (filter === 'all') return true
  if (filter === 'failed') return Boolean(opts.failed)
  if (filter === 'done') return Boolean(shot.current_assets?.keyframe)
  return !shot.current_assets?.keyframe
}

/** 卡片缩略图地址：优先缩略图，回落原图 */
export function shotThumbUrl(shot: Shot): string {
  const kf = shot.current_assets?.keyframe
  return kf?.thumb_url || kf?.url || ''
}
