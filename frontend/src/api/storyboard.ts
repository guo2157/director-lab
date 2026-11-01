import { post, del } from './client'
import type { Shot } from '@/types/script'
import type { GenerateShotPayload, GenerateResponse, BatchGenerateResponse } from '@/types/job'
import type { ManualAssetRole } from '@/types/asset'

/** 单镜头生成（target 枚举 keyframe/clip/audio/bgm/sfx；workflow_id 可选缺省自动匹配；返回 202 + job + effective_inputs） */
export function generateShot(shotId: string, payload: GenerateShotPayload) {
  return post<GenerateResponse>(`/shots/${shotId}/generate`, payload)
}

/** 单镜头重新生成 */
export function regenerateShot(shotId: string, payload: GenerateShotPayload) {
  return post<GenerateResponse>(`/shots/${shotId}/regenerate`, payload)
}
/** 重新生成分镜视频：先取消该镜头历史在途任务（释放 ComfyUI 资源）并覆盖原视频，返回新任务与被取消任务 ID */
export function regenerateShotClip(shotId: string, payload?: GenerateShotPayload) {
  return post<GenerateResponse & { cancelled_job_ids?: string[] }>(
    `/shots/${shotId}/regenerate-clip`,
    payload ?? { target: 'clip' },
  )
}

/** 场景内批量生成（<3 镜头返回 400；按 seq 升序，前镜视频成功抽末帧后才继续下一镜；worker 并发 = 可用 ComfyUI 实例数） */
export function generateSceneShots(sceneId: string, payload: {
  shot_ids?: string[]
  target: 'keyframe' | 'clip'
  workflow_id?: string
}) {
  return post<BatchGenerateResponse>(`/scenes/${sceneId}/generate`, payload)
}

/** 跨场景批量生成（顺序：集数升序→场景出场序→seq 升序；末帧衔接仅同场景同集内成立；worker 并发 = 可用 ComfyUI 实例数） */
export function generateProjectShots(projectId: string, payload: {
  shot_ids?: string[]
  episode_from?: number
  episode_to?: number
  scene_ids?: string[]
  target: 'keyframe' | 'clip'
  workflow_id?: string
  /** 批量出视频方式：independent 独立并行（不依赖上一镜尾帧）/ chained 链式串行（逐镜衔接） */
  chain_mode?: 'independent' | 'chained'
}) {
  return post<BatchGenerateResponse>(`/projects/${projectId}/generate`, payload)
}

/** 角色定妆图生成（target 固定 keyframe，成功回写 portrait_asset_id） */
export function generateCharacterPortrait(characterId: string, payload?: { workflow_id?: string }) {
  return post<GenerateResponse>(`/characters/${characterId}/generate`, payload)
}

/** 道具锚定图生成（target 固定 keyframe，成功回写 anchor_asset_id） */
export function generatePropAnchor(propId: string, payload?: { workflow_id?: string }) {
  return post<GenerateResponse>(`/props/${propId}/generate`, payload)
}

/** 场景基准图生成（回写 baseline_asset_id） */
export function generateSceneBaseline(sceneId: string, payload?: { workflow_id?: string }) {
  return post<GenerateResponse>(`/scenes/${sceneId}/generate-baseline`, payload)
}

/** 手动绑定资产（仅 4 种 role；单镜头资产 ≤6 个超限 400；重复绑定 409） */
export function bindShotAsset(shotId: string, payload: { asset_id: string; role: ManualAssetRole }) {
  return post<Shot>(`/shots/${shotId}/assets`, payload)
}

/** 解绑镜头资产 */
export async function unbindShotAsset(shotId: string, assetId: string) {
  await del<void>(`/shots/${shotId}/assets/${assetId}`)
}
