/**
 * 作品封面：从项目已有资产里挑一张最有「画面感」的图。
 *
 * 为什么需要：项目卡片此前只有首字水印 + 网格底纹的占位封面，
 * 四个作品排在一起时视觉上完全是「数据库记录卡」，没有任何创作内容可读。
 * Pixmax 一类创作平台的作品列表，第一落点一定是画面本身。
 *
 * 优先级（越靠前越能代表「这部剧的样子」）：
 *   1. keyframe  分镜图 —— 真实画面，最接近成片观感
 *   2. scene     场景基准图 —— 无分镜时的次选
 *   3. character 角色定妆图 —— 至少能看到主角
 * 都没有则返回空串，卡片回退到原有的占位封面（不伪造图片）。
 *
 * 只读现有 `/api/assets?project_id=` 接口，不新增后端通道、不改数据结构。
 */
import { useQueries } from '@tanstack/vue-query'
import { computed, type ComputedRef } from 'vue'
import { listAssets } from '@/api/assets'
import { assetImgUrl } from '@/components/assets/assetMedia'
import type { Asset } from '@/types/asset'

/** 单项目封面优先级：keyframe > scene > character */
const COVER_PRIORITY = ['keyframe', 'scene', 'character'] as const

/**
 * 过滤测试/占位产物。
 *
 * 实测库中存在 `[MOCK] job xxx out cover` 这类测试图：尺寸合规（400×225）、
 * thumb_url 正常、DB 里也没有可判别字段（asset 表无 size_bytes/mock 标记），
 * 但缩略图只有 1340 字节（真实分镜缩略图 10–14KB），渲染出来是一块深色带边框的假图。
 *
 * 前端拿不到文件体积，只能在图片加载后判定。这里做两件事：
 *   1. 名称层面的粗筛（成本为零）
 *   2. 加载后用 canvas 采样判断「是否近乎纯色」——见 ProjectCard 的 onCoverLoad
 * 拿不到判定结果时按可用处理（宁可显示一张普通图，也不无端隐藏真实封面）。
 */
function isUsableCover(a: Asset): boolean {
  if (!(a.thumb_url || a.url)) return false
  const name = String(a.name ?? '').toLowerCase()
  if (name.includes('mock') || name.includes('placeholder')) return false
  return true
}

function pickCover(items: Asset[]): string {
  for (const type of COVER_PRIORITY) {
    const hit = items
      .filter((a) => a.asset_type === type)
      .filter(isUsableCover)
      // 同类型取版本最新的一张：重新生成后封面应跟着变
      .sort((a, b) => String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')))[0]
    if (hit) return assetImgUrl(hit)
  }
  return ''
}

/**
 * 批量取多个项目的封面地址。
 * 用 useQueries 并行，每个项目一个 query key，与资产库页面共用缓存，
 * 进入项目后返回列表不会重复请求。
 */
export function useProjectCovers(projectIds: ComputedRef<string[]>): ComputedRef<Record<string, string>> {
  const queries = useQueries({
    queries: computed(() =>
      projectIds.value.map((id) => ({
        queryKey: ['assets', 'project', id, 'cover'] as const,
        queryFn: async () => {
          const res = await listAssets({ project_id: id, page: 1, page_size: 200 })
          const items = (Array.isArray(res) ? res : (res?.items ?? [])) as Asset[]
          return pickCover(items)
        },
        // 封面变化频率低，5 分钟内不重复请求
        staleTime: 5 * 60 * 1000,
      })),
    ),
  })

  return computed(() => {
    const map: Record<string, string> = {}
    projectIds.value.forEach((id, i) => {
      const url = queries.value[i]?.data
      if (url) map[id] = url
    })
    return map
  })
}
