import { get } from './client'

/** 阶段标识：与后端 stage_readiness.STAGE_* 一致 */
export type StageKey = 'assets' | 'storyboard_images' | 'storyboard_videos' | 'compose'

/** 阻断明细：资产类带 asset_type，镜头类带 shot_id/episode/seq */
export interface StageBlocker {
  /** 展示名：资产名（「游乐园入口」）或镜头名（「第 1 集 第 12 镜」） */
  name: string
  /** 阻断原因：asset_missing / asset_generating / asset_invalid / asset_file_missing
   *  / shot_missing_asset / shot_generating / shot_failed / shot_file_missing */
  reason: string
  detail?: string
  asset_id?: string | null
  entity_id?: string | null
  asset_type?: string
  asset_type_label?: string
  shot_id?: string
  episode?: number
  seq?: number
  scene_title?: string
}

/** 阶段就绪结果：ready 才是唯一跳转依据 */
export interface StageReadiness {
  stage: StageKey
  ready: boolean
  total: number
  done: number
  failed: number
  generating: number
  missing: StageBlocker[]
  blockers: StageBlocker[]
  message: string
}

/**
 * 阶段就绪检查（后端权威判定）。
 *
 * 与进度接口的区别：进度接口回答「做到哪了」（含在途/失败统计，用于轮询展示）；
 * 本接口回答「能不能进入下一阶段」——判定口径是「当前有效产物 + 文件真实存在」，
 * 不看任务历史，也不看任何人工审核状态。
 *
 * 前端点击阶段跳转按钮时先查本接口；确认弹窗点击确认后再查一次，
 * 防止弹窗打开期间资产/镜头状态发生变化（不允许用缓存里的旧 ready=true 绕过）。
 */
export function getStageReadiness(projectId: string, stage: StageKey) {
  return get<StageReadiness>(`/projects/${projectId}/stage-readiness/${stage}`)
}
