/**
 * 生产阶段枚举：唯一来源，对齐 backend/app/services/progress.py 的 current_stage 推导规则。
 * 顺序即生产顺序，`stageIndex` 依赖此顺序。
 */
export const STAGE_ORDER = [
  'estimate',
  'select_range',
  'parse',
  'characters',
  'scenes',
  'assets_review',
  'keyframes',
  'clips',
  'audio',
  'compose',
  'export',
  'done',
] as const

export type Stage = (typeof STAGE_ORDER)[number]

/**
 * 后端阶段枚举 → 中文阶段名。
 *
 * 2026-11-20：解析独立成制作节点后，这三个标签只描述「后端枚举此刻的含义」，
 * 不再暗示页面上存在对应的子步骤。枚举本身与 STAGE_ORDER 一律不动
 * （后端 derive_progress 依赖它，前端 stageIndex 也依赖它）。
 */
export const STAGE_LABELS: Record<string, string> = {
  estimate: '剧本编写',
  select_range: '待解析',
  parse: '解析中',
  characters: '角色定妆',
  scenes: '场景基准',
  assets_review: '资产审核',
  keyframes: '分镜出图',
  clips: '分镜成片',
  audio: '音频制作',
  compose: '合成',
  export: '导出',
  done: '已完成',
}

/** 未知枚举原样返回，避免把后端新增阶段吞成空串 */
export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return ''
  return STAGE_LABELS[stage] ?? stage
}

/** 阶段序号，未知阶段返回 -1 */
export function stageIndex(stage: string | null | undefined): number {
  if (!stage) return -1
  return (STAGE_ORDER as readonly string[]).indexOf(stage)
}
