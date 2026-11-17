import type { Shot } from '@/types/script'

/** 与 global.css 的 .status-tag.is-* 一一对应 */
export type StatusKind = 'pending' | 'running' | 'succeeded' | 'failed' | 'generating' | 'locked' | 'stale'

export interface ShotStatusView {
  kind: StatusKind
  label: string
}

/**
 * 镜头状态判定：在途 > 失败 > 未生成 > 已确认 > 待确认。
 * 卡片角标与详情页共用同一套判定，避免两处说法不一致。
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
  if (shot.keyframe_confirmed) return { kind: 'succeeded', label: '已完成' }
  return { kind: 'pending', label: '待确认' }
}

/** 状态筛选：全部 / 已完成（已确认）/ 待确认 / 失败 */
export type ShotFilter = 'all' | 'done' | 'pending' | 'failed'

export const SHOT_FILTERS: Array<{ value: ShotFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'done', label: '已完成' },
  { value: 'pending', label: '待确认' },
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
  if (filter === 'done') return Boolean(shot.keyframe_confirmed)
  return Boolean(shot.current_assets?.keyframe) && !shot.keyframe_confirmed
}

/** 卡片缩略图地址：优先缩略图，回落原图 */
export function shotThumbUrl(shot: Shot): string {
  const kf = shot.current_assets?.keyframe
  return kf?.thumb_url || kf?.url || ''
}
