import { get, post } from './client'
import type { ComposePayload, Composition, ExportPayload, ExportRecord } from '@/types/compose'

/** 发起合成（返回 202；校验失败 422：output_missing / duration_mismatch_review_required / speaker_mapping_required） */
export function composeProject(projectId: string, payload: ComposePayload) {
  return post<{ job_id: string; status: string }>(`/projects/${projectId}/compose`, payload)
}

/** 合成版本列表（按 created_at 降序，含 stale 标记） */
export function listCompositions(projectId: string) {
  return get<{ total: number; items: Composition[] }>(`/projects/${projectId}/compositions`).then(res => res.items)
}

/** 合成版本详情 */
export function getComposition(projectId: string, compId: string) {
  return get<Composition>(`/projects/${projectId}/compositions/${compId}`)
}

/** 发起导出（composition_id 必填须有效未过期；来源过期 409 composition_stale；幂等键防重复提交） */
export function exportProject(projectId: string, payload: ExportPayload) {
  return post<{ job_id: string; status: string }>(`/projects/${projectId}/export`, payload)
}

/** 导出记录列表（含 download_url：/api/exports/{id}/download 完整路径） */
export function listExports(projectId: string) {
  return get<{ total: number; items: ExportRecord[] }>(`/projects/${projectId}/exports`).then(res => res.items)
}

/** 下载导出文件（流式，Content-Disposition: attachment，不存在 404） */
export function downloadExportUrl(exportId: string) {
  return `/api/exports/${exportId}/download`
}
