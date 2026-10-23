import { get, post } from './client'
import type { Paginated, PaginationParams } from '@/types/common'
import type { Job, JobStats, JobType, JobStatus } from '@/types/job'

/** 任务列表（增量补齐以 updated_since_seq 为准：seq 严格大于，升序；updated_since 仅兼容） */
export function listJobs(params?: PaginationParams & {
  project_id?: string
  job_type?: JobType
  status?: JobStatus
  updated_since_seq?: number
  updated_since?: string
}) {
  return get<Paginated<Job>>('/jobs', params)
}

/** 任务统计（avg_duration_seconds 近 7 日，无数据 null） */
export function getJobStats() {
  return get<JobStats>('/jobs/stats')
}

/** 任务详情 */
export function getJob(id: string) {
  return get<Job>(`/jobs/${id}`)
}

/** 重试任务（仅 failed/lost 且 retryable=true，生成新任务 ID） */
export function retryJob(id: string) {
  return post<Job>(`/jobs/${id}/retry`)
}

/** 取消任务（仅 queued/running；running 调 ComfyUI 全局中断） */
export function cancelJob(id: string) {
  return post<Job>(`/jobs/${id}/cancel`)
}
