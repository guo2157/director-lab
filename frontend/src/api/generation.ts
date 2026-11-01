import { get } from './client'
import type {
  AssetGenerationProgress,
  ClipGenerationProgress,
  KeyframeGenerationProgress,
} from '@/types/generation'

/**
 * 公共资产图生成进度（角色定妆图 / 场景基准图 / 道具锚定图）。
 * 返回总体 done/total、在途明细（正在生成什么）与最近失败，供资产库页轮询。
 */
export function getAssetGenerationProgress(projectId: string) {
  return get<AssetGenerationProgress>(`/projects/${projectId}/asset-generation-progress`)
}

/**
 * 分镜图（逐镜 keyframe）生成进度。
 * 返回全片镜头总数、已出图数、确认数、在途明细与最近失败，供分镜图页轮询。
 */
export function getKeyframeGenerationProgress(projectId: string) {
  return get<KeyframeGenerationProgress>(`/projects/${projectId}/keyframe-generation-progress`)
}

/**
 * 分镜视频生成进度（分镜看板）。
 * 额外返回排队名次、近 7 日平均单镜耗时与剩余队列预计耗时，链式批量下也能看到「还要等多久」。
 */
export function getClipGenerationProgress(projectId: string) {
  return get<ClipGenerationProgress>(`/projects/${projectId}/clip-generation-progress`)
}
