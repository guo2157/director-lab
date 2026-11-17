/** 资产图片地址：追加 updated_at 版本参数，替换/更新后自动换图，避免浏览器缓存旧图。 */
import type { Asset } from '@/types/asset'

export function assetImgUrl(asset: Pick<Asset, 'thumb_url' | 'url' | 'updated_at'> | null | undefined): string {
  if (!asset) return ''
  const base = asset.thumb_url || asset.url || ''
  if (!base) return ''
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}v=${encodeURIComponent(asset.updated_at ?? '')}`
}
