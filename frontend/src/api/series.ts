import { get, post, patch, put } from './client'
import type {
  ContextManifest,
  ContextPreview,
  ContinuePayload,
  ContinueResult,
  ContinuityCheckResult,
  ContinuityOverview,
  ContinuitySnapshot,
  EpisodeAssetRefView,
  EpisodePage,
  PlotThread,
  PlotThreadStatus,
  ScriptConfirmPayload,
  ScriptConfirmResult,
  SeriesAssetView,
  SeriesBible,
  SeriesCreatePayload,
  SeriesEpisode,
  SeriesPage,
  SeriesScript,
  SeriesUpdatePayload,
  SeriesListItem,
  StoryEvent,
} from '@/types/series'

/** 系列列表（分页 + 关键词/状态过滤） */
export function listSeries(params?: { keyword?: string; status?: string; page?: number; page_size?: number }) {
  return get<SeriesPage>('/series', params)
}

/** 系列详情（含真实派生统计） */
export function getSeries(id: string) {
  return get<SeriesListItem>(`/series/${id}`)
}

/** 创建系列（同时建立 Series Bible V1） */
export function createSeries(payload: SeriesCreatePayload) {
  return post<SeriesListItem>('/series', payload)
}

/** 更新系列基础信息 */
export function updateSeries(id: string, payload: SeriesUpdatePayload) {
  return patch<SeriesListItem>(`/series/${id}`, payload)
}

/** 归档系列（不删除历史资产与生产记录） */
export function archiveSeries(id: string) {
  return post<SeriesListItem>(`/series/${id}/archive`)
}

/** 恢复归档系列 */
export function unarchiveSeries(id: string) {
  return post<SeriesListItem>(`/series/${id}/unarchive`)
}

/** 当前激活的 Series Bible */
export function getSeriesBible(id: string) {
  return get<SeriesBible>(`/series/${id}/bible`)
}

/** 保存 Bible 为新版本（不覆盖历史版本） */
export function saveSeriesBible(id: string, payload: { content: Record<string, unknown>; change_summary?: string | null }) {
  return put<SeriesBible>(`/series/${id}/bible`, payload)
}

/** Bible 版本历史 */
export function listSeriesBibleVersions(id: string) {
  return get<SeriesBible[]>(`/series/${id}/bible/versions`)
}

/** 剧集列表 */
export function listEpisodes(seriesId: string, params?: { page?: number; page_size?: number }) {
  return get<EpisodePage>(`/series/${seriesId}/episodes`, params)
}

/** 创建剧集（不传集号则自动取下一个） */
export function createEpisode(seriesId: string, payload: { episode_number?: number | null; title?: string | null; synopsis?: string | null }) {
  return post<SeriesEpisode>(`/series/${seriesId}/episodes`, payload)
}

/** 剧集详情 */
export function getEpisode(id: string) {
  return get<SeriesEpisode>(`/episodes/${id}`)
}

/** 下一集号 */
export function getNextEpisodeNumber(seriesId: string) {
  return get<{ next_episode_number: number }>(`/series/${seriesId}/next-episode-number`)
}

/** 剧集工作区（含绑定的生产项目入口） */
export function getEpisodeWorkspace(id: string) {
  return get<Record<string, unknown>>(`/episodes/${id}/workspace`)
}

/** 创建续写任务（返回 202 与 task_id，不等待剧本生成） */
export function continueEpisode(seriesId: string, payload: ContinuePayload) {
  return post<ContinueResult>(`/series/${seriesId}/episodes/continue`, payload)
}

/** 上下文预览（与正式生成同一构建规则） */
export function previewContext(seriesId: string, params: { target_episode_number: number; user_instruction?: string }) {
  return get<ContextPreview>(`/series/${seriesId}/context-preview`, params)
}

/** 最近一次上下文清单 */
export function getContextManifest(episodeId: string) {
  return get<ContextManifest>(`/episodes/${episodeId}/context-manifest`)
}

/** 剧本版本列表 */
export function listScripts(episodeId: string) {
  return get<SeriesScript[]>(`/episodes/${episodeId}/scripts`)
}

/** 保存剧本草稿（总是新建版本） */
export function saveScript(episodeId: string, payload: { content: string; synopsis?: string | null }) {
  return post<SeriesScript>(`/episodes/${episodeId}/scripts`, payload)
}

/** 确认剧本（提交正式剧情状态变更） */
export function confirmScript(episodeId: string, scriptId: string, payload: ScriptConfirmPayload) {
  return post<ScriptConfirmResult>(`/episodes/${episodeId}/scripts/${scriptId}/confirm`, payload)
}

/** 重新生成剧本（创建新版本，不覆盖已确认版本） */
export function regenerateScript(episodeId: string, scriptId: string, payload: ContinuePayload) {
  return post<ContinueResult>(`/episodes/${episodeId}/scripts/${scriptId}/regenerate`, payload)
}

/** 剧情事件时间线 */
export function listStoryEvents(seriesId: string, params?: { episode_id?: string; fact_status?: string; limit?: number }) {
  return get<StoryEvent[]>(`/series/${seriesId}/story-events`, params)
}

/** 连续性概览 */
export function getContinuityOverview(seriesId: string) {
  return get<ContinuityOverview>(`/series/${seriesId}/continuity`)
}

/** 连续性快照历史 */
export function listContinuitySnapshots(seriesId: string, params?: { limit?: number }) {
  return get<ContinuitySnapshot[]>(`/series/${seriesId}/continuity/snapshots`, params)
}

/** 对指定剧集执行连续性检查 */
export function checkContinuity(episodeId: string) {
  return post<ContinuityCheckResult>(`/episodes/${episodeId}/continuity/check`)
}

/** 剧本版本的连续性问题列表 */
export function listContinuityIssues(episodeId: string) {
  return get<{ status: string; issues: Array<Record<string, unknown>>; script_version_id: string | null }>(
    `/episodes/${episodeId}/continuity/issues`,
  )
}

/** 剧情线列表 */
export function listPlotThreads(seriesId: string, params?: { status?: PlotThreadStatus }) {
  return get<PlotThread[]>(`/series/${seriesId}/plot-threads`, params)
}

/** 新建剧情线 */
export function createPlotThread(seriesId: string, payload: { title: string; description?: string | null; introduced_episode_id?: string | null; resolution_condition?: string | null }) {
  return post<PlotThread>(`/series/${seriesId}/plot-threads`, payload)
}

/** 更新剧情线（状态流转由用户显式操作） */
export function updatePlotThread(threadId: string, payload: { title?: string; description?: string | null; status?: PlotThreadStatus; last_updated_episode_id?: string | null; resolution_condition?: string | null }) {
  return patch<PlotThread>(`/plot-threads/${threadId}`, payload)
}

/** 系列共享资产列表 */
export function listSeriesAssets(seriesId: string, params?: { asset_type?: string }) {
  return get<SeriesAssetView[]>(`/series/${seriesId}/assets`, params)
}

/** 把既有资产登记为系列共享资产（不复制、不改归属） */
export function registerSeriesAsset(seriesId: string, params: { asset_id: string; first_episode_id?: string }) {
  const query = new URLSearchParams({ asset_id: params.asset_id })
  if (params.first_episode_id) query.set('first_episode_id', params.first_episode_id)
  return post<Record<string, unknown>>(`/series/${seriesId}/assets?${query.toString()}`)
}

/** 某集实际使用的资产版本 */
export function listEpisodeAssets(episodeId: string) {
  return get<EpisodeAssetRefView[]>(`/episodes/${episodeId}/assets`)
}

/** 资产匹配建议（名称匹配仅为提示，需人工确认） */
export function resolveEpisodeAssets(episodeId: string) {
  return post<{ episode_id: string; script_version_id: string | null; candidates: Record<string, Array<Record<string, unknown>>>; note: string }>(
    `/episodes/${episodeId}/assets/resolve`,
  )
}

/** 资产使用历史 */
export function getAssetUsage(seriesId: string, assetId: string) {
  return get<Record<string, unknown>>(`/series/${seriesId}/assets/${assetId}/usage`)
}
