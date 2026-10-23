import { get, post, del } from './client'
import { apiClient } from './client'
import type { Paginated, PaginationParams } from '@/types/common'
import type { Asset, AssetVersion } from '@/types/asset'

/** 资产列表（分页每页 20 条） */
export function listAssets(params?: PaginationParams & {
  project_id?: string
  asset_type?: string
  status?: string
  keyword?: string
}) {
  return get<Paginated<Asset>>('/assets', params)
}

/** 资产详情 */
export function getAsset(id: string) {
  return get<Asset>(`/assets/${id}`)
}

/** 资产版本时间线 */
export function listAssetVersions(id: string) {
  return get<AssetVersion[]>(`/assets/${id}/versions`)
}

/** 上传资产（单文件 ≤500MB，multipart） */
export function uploadAsset(file: File, payload: { project_id: string; asset_type?: string; name?: string }) {
  const form = new FormData()
  form.append('file', file)
  form.append('project_id', payload.project_id)
  if (payload.asset_type) form.append('asset_type', payload.asset_type)
  if (payload.name) form.append('name', payload.name)
  return apiClient.post<Asset>('/assets/upload', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  }).then((res) => res.data)
}

/** 锁定资产（锁定资产删除禁用） */
export function lockAsset(id: string) {
  return post<Asset>(`/assets/${id}/lock`)
}

/** 解锁资产 */
export function unlockAsset(id: string) {
  return post<Asset>(`/assets/${id}/unlock`)
}

/** 回滚资产版本（传 target_version，生成新版本，镜头标记待重做但不自动触发任务） */
export function rollbackAsset(id: string, targetVersion: number) {
  return post<Asset>(`/assets/${id}/rollback`, { target_version: targetVersion })
}

/** 删除资产 */
export async function deleteAsset(id: string) {
  await del<void>(`/assets/${id}`)
}

/** 批量删除（任一锁定或引用则整批 409，不部分执行） */
export function batchDeleteAssets(ids: string[]) {
  return post<{ deleted: number }>('/assets/batch/delete', { ids })
}

/** 批量锁定 */
export function batchLockAssets(ids: string[]) {
  return post<{ locked: number }>('/assets/batch/lock', { ids })
}

/** 批量解锁 */
export function batchUnlockAssets(ids: string[]) {
  return post<{ unlocked: number }>('/assets/batch/unlock', { ids })
}

/** 导出资产打包（POST 避免 URL 过长，返回打包下载流） */
export function exportAssets(ids: string[]) {
  return apiClient.post('/assets/export', { ids }, { responseType: 'blob' }).then((res) => res.data as Blob)
}
