/** 资产卡片的统一视图模型：实体（脚本角色/场景/道具）+ 已绑定资产。 */
import type { Asset } from '@/types/asset'

export type AssetKind = 'character' | 'scene' | 'prop'

export interface AssetCardItem {
  /** 列表 key（实体卡与孤立资产卡不会撞） */
  key: string
  kind: AssetKind
  /** 脚本实体 ID；孤立资产（库里存在但脚本无对应实体）为 null，仅可查看/替换 */
  entityId: string | null
  name: string
  /** 一句描述，取自实体 description；孤立资产为空 */
  desc: string
  /** 实体是否已有资产图（与后端齐全度同口径） */
  done: boolean
  /** 已绑定资产：图片、详情、替换、重生成、锁定/删除都基于它 */
  asset: Asset | null
}

export const ASSET_KIND_LABELS: Record<AssetKind, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
}

/** 资产图类型名：用于「生成定妆图 / 基准图 / 锚定图」这类固定说法 */
export const ASSET_UNIT_LABELS: Record<AssetKind, string> = {
  character: '定妆图',
  scene: '基准图',
  prop: '锚定图',
}

/** 本批任务进度（工具栏内联展示） */
export interface BatchProgressView {
  total: number
  done: number
  running: number
  failed: number
  percent: number
}

export interface ProgressGroupView {
  label: string
  done: number
  total: number
}
