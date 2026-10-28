/** 画幅比枚举（API.md：9:16 / 16:9 / 1:1） */
export type AspectRatio = '9:16' | '16:9' | '1:1'

/** 创作类型枚举（API.md：novel=小说改编 / script=剧本直出，默认 novel） */
export type CreationType = 'novel' | 'script'

/** 项目状态：初始 draft */
export type ProjectStatus = 'draft' | 'active' | 'archived'

/** 项目（ID 前缀 prj_，时间 ISO 8601 含时区） */
export interface Project {
  id: string
  name: string
  aspect_ratio: AspectRatio
  creation_type: CreationType
  status: ProjectStatus
  episode_plan?: EpisodePlanItem[] | null
  settings?: ProjectSettings
  created_at: string
  updated_at: string
  /** 派生字段：镜头总数 */
  shot_total?: number
  /** 派生字段：已完成镜头数 */
  shot_done?: number
  /** 派生字段：当前生产阶段（estimate/parse/characters/scenes/keyframes/clips/compose/export/done 等） */
  current_stage?: string
  /** 当前解析版本 ID（乐观锁） */
  current_parse_version_id?: string | null
  /** 公共资产人工审核状态：pending 待审核 / approved 通过 / rejected 驳回 */
  assets_review_status?: 'pending' | 'approved' | 'rejected'
  /** 公共资产人工审核时间 */
  assets_reviewed_at?: string | null
}

/** 集数规划条目（job.finished result.episodes 写入 project.episode_plan） */
export interface EpisodePlanItem {
  episode: number
  title?: string
  summary?: string
  shot_count?: number
  duration_target?: number
}

/** 项目产能与一致性设置（PATCH /api/projects/{id}/settings） */
export interface ProjectSettings {
  /** 一致性：fail_threshold 须小于 pass_threshold，否则 400 */
  consistency_pass_threshold?: number
  consistency_fail_threshold?: number
  /** 产能参数 */
  episode_duration_target?: number
  shots_per_episode_target?: number
  shot_duration_default?: number
  max_episodes_per_batch?: number
}

/** 新建项目参数：name 1–100 字符必填 */
export interface CreateProjectPayload {
  name: string
  aspect_ratio: AspectRatio
  creation_type?: CreationType
}

/** 更新项目参数 */
export interface UpdateProjectPayload {
  name?: string
  settings?: ProjectSettings
}
