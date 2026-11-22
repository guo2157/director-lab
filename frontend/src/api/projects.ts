import { get, post, patch, put, del } from './client'
import type { Paginated, PaginationParams } from '@/types/common'
import type { Project,
  CreateProjectPayload,
  UpdateProjectPayload,
  StyleConfig,
  StyleOptions,
  ProjectStyleConfigResponse,
  SaveStyleConfigResponse,
} from '@/types/project'
import type { ScriptVersion, ScriptEstimate, ParseVersion, CreateScriptVersionPayload, ParsePayload, ParseStatus } from '@/types/script'

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

/** 删除项目（软删除）
 *  force=true 时服务端会先取消全部在途任务再删除（前端二次确认后使用）。 */
export async function deleteProject(id: string, opts?: { force?: boolean }) {
  await del<void>(`/projects/${id}`, opts?.force ? { force: true } : undefined)
}

/** 硬删除影响面预览：返回将被清理的数据量与阻塞任务数 */
export interface PurgePreview {
  project_id: string
  project_name: string
  blocking_jobs: number
  deletable: boolean
  counts: {
    scenes: number
    shots: number
    assets: number
    jobs: number
    characters: number
    props: number
    script_versions: number
  }
}
export function getPurgePreview(id: string) {
  return get<PurgePreview>(`/projects/${id}/purge-preview`)
}

/** 硬删除结果 */
export interface PurgeResult {
  project_id: string
  project_name: string
  deleted: Record<string, number>
  preview: PurgePreview['counts']
  media: { removed: number; missing: number; skipped: number }
  /** 本次连带取消的在途任务（force=true 且存在任务时才有意义） */
  cancelled: {
    jobs: number
    running: number
    interrupted: number
    tasks: number
    agent_tasks: number
  }
}

/** 硬删除项目：彻底清理全部关联数据与媒体文件，不可恢复。
 *  opts.force=true 时服务端先取消全部在途任务再删除（前端二次确认后使用）。 */
export async function purgeProject(id: string, opts?: { force?: boolean }) {
  return del<PurgeResult>(`/projects/${id}/purge`, opts?.force ? { force: true } : undefined)
}

/** 归档项目 */
export function archiveProject(id: string) {
  return post<Project>(`/projects/${id}/archive`)
}

/** 恢复归档项目 */
export function unarchiveProject(id: string) {
  return post<Project>(`/projects/${id}/unarchive`)
}

/** 剧本规范化校验结果：优化稿 + 变更清单 + 机器校验 */
export interface ScriptNormalizeChange {
  category: 'format' | 'clarity' | 'completion' | string
  summary: string
  evidence: string
  ai_generated: boolean
}

export interface ScriptNormalizeResult {
  normalized_text: string
  level: number
  changes: ScriptNormalizeChange[]
  questions: string[]
  checks: {
    characters_preserved: boolean
    dialogue_preserved: boolean
    missing_characters: string[]
    missing_dialogues: string[]
    /** 仅标点或空白差异的台词：提示用，不阻断采用 */
    punct_warnings?: string[]
    blocked: boolean
    /** 校验基准来源计数：正则从原文抽取 vs 模型自报，便于排查漏抽与漏报 */
    baseline?: {
      regex_characters: number
      regex_dialogues: number
      llm_dialogues: number
    }
  }
  original_chars: number
  normalized_chars: number
}

/** 剧本规范化校验（保存前预览）：不落库，返回优化稿与变更清单供用户确认 */
export function normalizeScript(projectId: string, payload: { source_text: string }) {
  return post<ScriptNormalizeResult>(`/projects/${projectId}/script-normalize`, payload, { timeout: 180000 })
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

/** 解析剧本（生成 candidate 候选版本，返回 job_id/status，SSE job.finished 通知；episode_count/episode_end 二选一） */
export function parseScript(projectId: string, payload: ParsePayload) {
  return post<{ job_id: string; status: string }>(`/projects/${projectId}/parse`, payload)
}
/** 剧本一体化分析（Agent）：评估+解析合并，集数由 Agent 决定，无需用户确认集数。
 *  force=true 跳过后端幂等复用（「重新解析」必须用它，否则拿回的还是旧结果）。 */
export function runScriptAgent(projectId: string, scriptVersionId: string, text: string, episodeStart?: number, force?: boolean) {
  return post<{ job_id: string; status: string; idempotent_reuse?: boolean }>(`/projects/${projectId}/script-agent`, {
    script_version_id: scriptVersionId,
    text,
    ...(episodeStart && episodeStart > 1 ? { episode_start: episodeStart } : {}),
    ...(force ? { force: true } : {}),
  })
}
/** Agent 分析状态查询（job + 口径 + 集级记录） */
export function getScriptAgentStatus(projectId: string) {
  return get<{
    job: { id: string; status: string; error_code: string | null; error_msg: string | null; result: Record<string, unknown> | null } | null
    scope: {
      total_episodes: number | null
      script_hash: string | null
      parse_version_id: string | null
      parse_version_status: string | null
      pending_persist_episodes?: number[]
      /** 实体表落库计数：判定「解析完成」是否真的可进入下一步 */
      persisted?: {
        scenes: number
        characters: number
        props: number
        shots: number
        episodes_with_shots: number[]
      }
    }
    records: Array<{ episode_no: number; status: string; asset_count: number | null; error_msg: string | null }>
  }>(`/projects/${projectId}/script-agent/status`)
}
/** 集级解析状态查询（集数网格视图数据源） */
export function getParseStatus(projectId: string) {
  return get<ParseStatus>(`/projects/${projectId}/parse/status`)
}
/** 重试失败的集级解析记录（仅 failed 集重新入队） */
export function retryParse(projectId: string) {
  return post<{ job_id: string; retried_episodes: number[] }>(`/projects/${projectId}/parse/retry`)
}

/** 确认解析版本（乐观锁，冲突返回 409 parse_version_conflict） */
export function confirmParseVersion(projectId: string, parseVersionId: string, currentParseVersionId?: string | null) {
  return post<ParseVersion>(`/projects/${projectId}/parse-versions/${parseVersionId}/confirm`, {
    current_parse_version_id: currentParseVersionId ?? null,
  })
}


/** 风格配置可选项（展示名 / 说明 / 默认值由后端下发，前端不硬编码） */
export function getStyleOptions() {
  return get<StyleOptions>("/projects/style-options")
}

/** 读取项目风格配置（含规范化配置、原始取值与校验问题） */
export function getProjectStyleConfig(projectId: string) {
  return get<ProjectStyleConfigResponse>(`/projects/${projectId}/style-config`)
}

/** 保存项目风格配置（仅影响后续任务，不自动重生成已审核资产与已生成结果） */
export function saveProjectStyleConfig(projectId: string, config: StyleConfig) {
  return put<SaveStyleConfigResponse>(`/projects/${projectId}/style-config`, { config })
}


