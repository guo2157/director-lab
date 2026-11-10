/** 系列化创作与续集管理类型：与后端 app/schemas/series.py 字段保持一致 */

/** 剧本状态（与生产状态独立） */
export type ScriptStatus =
  | 'draft'
  | 'analyzing'
  | 'awaiting_review'
  | 'confirmed'
  | 'superseded'
  | 'rejected'

/** 生产状态 */
export type ProductionStatus =
  | 'not_started'
  | 'queued'
  | 'running'
  | 'reviewing'
  | 'completed'
  | 'failed'
  | 'cancelled'

/** 剧情连续性状态（叙事维度，不是视频帧连续性） */
export type EpisodeContinuityStatus =
  | 'not_checked'
  | 'checking'
  | 'consistent'
  | 'warning'
  | 'conflict'
  | 'needs_review'

/** 剧情线状态 */
export type PlotThreadStatus = 'open' | 'in_progress' | 'resolved' | 'abandoned'

/** 事件事实状态：只有 confirmed 可作为正式剧情事实 */
export type StoryEventFactStatus =
  | 'confirmed'
  | 'derived'
  | 'uncertain'
  | 'proposed'
  | 'contradicted'

export interface Series {
  id: string
  name: string
  description: string | null
  cover_image: string | null
  genre: string | null
  style: string | null
  target_audience: string | null
  language: string
  status: 'active' | 'archived'
  mainline_id: string
  latest_confirmed_episode_id: string | null
  latest_completed_episode_id: string | null
  state_version: number
  created_at: string
  updated_at: string
}

/** 列表项附带派生统计（全部来自真实聚合，缺失为 0） */
export interface SeriesListItem extends Series {
  episode_count: number
  confirmed_script_count: number
  completed_video_count: number
  open_plot_thread_count: number
  bible_version: number
}

export interface SeriesPage {
  items: SeriesListItem[]
  total: number
  page: number
  page_size: number
}

export interface SeriesCreatePayload {
  name: string
  description?: string | null
  genre?: string | null
  style?: string | null
  target_audience?: string | null
  language?: string
  bible?: Record<string, unknown> | null
}

export interface SeriesUpdatePayload {
  name?: string
  description?: string | null
  genre?: string | null
  style?: string | null
  target_audience?: string | null
  language?: string
}

export interface SeriesBible {
  id: string
  series_id: string
  version: number
  content: Record<string, unknown> | null
  change_summary: string | null
  created_by: string | null
  created_at: string
  is_active: boolean
}

export interface SeriesEpisode {
  id: string
  series_id: string
  project_id: string | null
  project_episode_no: number | null
  episode_number: number
  title: string | null
  synopsis: string | null
  script_status: ScriptStatus
  production_status: ProductionStatus
  script_current_version_id: string | null
  confirmed_script_version_id: string | null
  context_manifest_id: string | null
  continuity_status: EpisodeContinuityStatus
  mainline_id: string
  created_at: string
  updated_at: string
}

export interface EpisodePage {
  items: SeriesEpisode[]
  total: number
  page: number
  page_size: number
}

export interface SeriesScript {
  id: string
  episode_id: string
  version: number
  content: string | null
  synopsis: string | null
  source_context_manifest_id: string | null
  prompt_template_version: string | null
  model: string | null
  validation_result: Record<string, unknown> | null
  continuity_result: Record<string, unknown> | null
  status: ScriptStatus
  created_at: string
  created_by: string | null
}

/** 连续性问题项（结构化，可定位来源事件） */
export interface ContinuityIssue {
  severity: 'error' | 'warning' | 'info'
  type: string
  asset_id: string | null
  message: string
  source_event_ids: string[]
  suggestion: string | null
}

export interface ContinuityCheckResult {
  status: 'consistent' | 'warning' | 'conflict'
  issues: ContinuityIssue[]
}

export interface StoryEvent {
  id: string
  series_id: string
  episode_id: string
  script_version_id: string
  event_key: string
  event_type: string
  title: string
  description: string | null
  involved_asset_ids: string[] | null
  location_asset_id: string | null
  source_scene_id: string | null
  source_text: string | null
  fact_status: StoryEventFactStatus
  importance: number
  occurred_order: number | null
  created_at: string
}

export interface ContinuitySnapshot {
  id: string
  series_id: string
  mainline_id: string
  version: number
  based_on_episode_id: string | null
  based_on_script_version_id: string | null
  state_json: Record<string, unknown> | null
  state_hash: string | null
  created_at: string
}

export interface ContinuityOverview {
  series_id: string
  state_version: number
  latest_snapshot: ContinuitySnapshot | null
  open_plot_threads: number
  confirmed_event_count: number
  episodes_with_conflict: number
}

export interface PlotThread {
  id: string
  series_id: string
  title: string
  description: string | null
  introduced_episode_id: string | null
  last_updated_episode_id: string | null
  status: PlotThreadStatus
  related_asset_ids: string[] | null
  related_event_ids: string[] | null
  resolution_condition: string | null
  created_at: string
  updated_at: string | null
}

export interface ContextManifest {
  id: string
  series_id: string
  episode_id: string
  context_version: number
  manifest_json: Record<string, unknown> | null
  context_hash: string | null
  prompt_template_version: string | null
  model: string | null
  created_at: string
}

/** 续写上下文预览结果（与后端 Context Builder 输出一致） */
export interface ContextPreview {
  series: Record<string, unknown>
  target_episode_number: number
  series_bible: { version: number; content: Record<string, unknown> }
  continuity: {
    snapshot_version: number | null
    snapshot_id: string | null
    state: Record<string, unknown> | null
  }
  previous_episode: Record<string, unknown> | null
  plot_threads: Array<Record<string, unknown>>
  history_events: Array<Record<string, unknown>>
  assets: Array<Record<string, unknown>>
  user_instruction: string
  constraints: Record<string, unknown>
  warnings: Array<{ code: string; message: string }>
  missing: Array<{ code: string; message: string; [k: string]: unknown }>
  context_hash: string
}

export interface ContinuePayload {
  user_instruction: string
  direction?: 'continue' | 'advance_thread' | 'new_event' | 'custom'
  constraints?: {
    reuse_assets?: boolean
    preserve_character_identity?: boolean
    avoid_story_conflicts?: boolean
  }
  target_episode_number?: number | null
  title?: string | null
}

export interface ContinueResult {
  episode_id: string
  episode_number: number
  task_id: string
  status: string
}

export interface ScriptConfirmPayload {
  expected_series_state_version: number
  review_note?: string | null
  ignore_warnings?: boolean
}

export interface ScriptConfirmResult {
  episode_id: string
  script_version_id: string
  script_status: string
  series_state_version: number
  snapshot_version: number
  committed_event_count: number
  issues: ContinuityIssue[]
  ignored_issue_ids: string[]
  idempotent_replay?: boolean
}

export interface EpisodeAssetRefView {
  id: string
  episode_id: string
  asset_id: string
  asset_version: number
  usage_type: string
  is_required: boolean
  source: string | null
  created_at: string
  asset_name: string
  asset_type: string
  current_version: number
  version_drift: boolean
}

export interface SeriesAssetView {
  id: string
  series_id: string
  asset_id: string
  asset_name: string
  asset_type: string
  current_version: number
  file_path: string | null
  thumb_path: string | null
  first_episode_id: string | null
  created_at: string
}
