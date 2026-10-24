import { get, post, patch, del } from './client'
import type { Paginated, PaginationParams } from '@/types/common'
import type { Project, CreateProjectPayload, UpdateProjectPayload } from '@/types/project'
import type { ScriptVersion, ScriptEstimate, ParseVersion, CreateScriptVersionPayload, ParsePayload } from '@/types/script'

/** 项目列表（分页） */
export function listProjects(params?: PaginationParams & { status?: string; keyword?: string }) {
  return get<Paginated<Project>>('/projects', params)
}

/** 项目详情 */
export function getProject(id: string) {
  return get<Project>(`/projects/${id}`)
}

/** 新建项目（name 1–100 字符必填；成功初始 status=draft） */
export function createProject(payload: CreateProjectPayload) {
  return post<Project>('/projects', payload)
}

/** 更新项目（名称/设置） */
export function updateProject(id: string, payload: UpdateProjectPayload) {
  return patch<Project>(`/projects/${id}`, payload)
}

/** 删除项目（有进行中任务返回 409；成功软删除 204） */
export async function deleteProject(id: string) {
  await del<void>(`/projects/${id}`)
}

/** 归档项目 */
export function archiveProject(id: string) {
  return post<Project>(`/projects/${id}/archive`)
}

/** 恢复归档项目 */
export function unarchiveProject(id: string) {
  return post<Project>(`/projects/${id}/unarchive`)
}

/** 新建剧本版本（text 1–50000 字符，返回 script_version_id/version/content_hash） */
export function createScriptVersion(projectId: string, payload: CreateScriptVersionPayload) {
  return post<ScriptVersion>(`/projects/${projectId}/script-versions`, payload)
}

/** 获取当前剧本（单版本覆盖模式，供页面回显） */
export function getCurrentScript(projectId: string) {
  return get<{ exists: boolean; text: string; script_version_id: string | null; version: number | null }>(`/projects/${projectId}/script-versions/current`)
}

/** 剧本估算（须与 script_version 哈希一致，结果持久化到 script_estimate） */
export function estimateScript(projectId: string, scriptVersionId: string, text: string) {
  return post<ScriptEstimate>(`/projects/${projectId}/estimate`, { script_version_id: scriptVersionId, text })
}

/** 确认估算（版本不匹配返回 409 script_version_conflict；episode_start/episode_count 为确认的制作范围） */
export function confirmEstimate(
  projectId: string,
  estimateId: string,
  payload: { script_version_id: string; episode_start: number; episode_count: number },
) {
  return post<ScriptEstimate>(`/projects/${projectId}/estimates/${estimateId}/confirm`, payload)
}

/** 解析剧本（生成 candidate 候选版本，返回 job_id，SSE job.finished 通知；text/episode_start/episode_count 必填） */
export function parseScript(projectId: string, payload: ParsePayload) {
  return post<{ job_id: string; parse_version: ParseVersion }>(`/projects/${projectId}/parse`, payload)
}

/** 确认解析版本（乐观锁，冲突返回 409 parse_version_conflict） */
export function confirmParseVersion(projectId: string, parseVersionId: string) {
  return post<ParseVersion>(`/projects/${projectId}/parse-versions/${parseVersionId}/confirm`)
}
