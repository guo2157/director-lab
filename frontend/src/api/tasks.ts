import { get } from './client'

/** 任务阶段（后端 task_orchestrator.task_payload.stages） */
export interface TaskStageInfo {
  stage_id: string
  stage_index: number
  stage_key: string
  stage_name: string
  status: string
  progress: number
  job_id: string | null
}

/** 任务卡片数据（后端 assistant payload.task_card，覆盖 parse/generate/compose/export） */
export interface TaskCardData {
  task_id: string
  task_type: string
  title: string
  status: string
  progress: number
  current_message?: string | null
  error_code: string | null
  error_msg: string | null
  stages: TaskStageInfo[]
  result: Record<string, unknown> | null
}

/** 统一任务事件（/api/tasks/events SSE 与 backfill，seq 严格递增） */
export interface TaskEvent {
  seq: number
  event_type: string
  task_id: string
  status: string | null
  progress: number | null
  updated_at: string | null
  [key: string]: unknown
}

/** 任务详情（GET /api/tasks/{task_id}）：刷新或切换会话后按 task_id 恢复真实进度 */
export interface TaskDetail extends TaskCardData {
  project_id: string | null
  conversation_id: string | null
  message_id: string | null
  current_step: string | null
  current_message: string | null
  started_at: string | null
  updated_at: string | null
  /** 耗时秒数，由后端 started_at 推导，前端不得自行累加 */
  elapsed_seconds: number
}

/** 按 task_id 查询任务真实详情 */
export function getTask(taskId: string) {
  return get<TaskDetail>(`/tasks/${taskId}`)
}

/** 任务事件断线补齐：seq 严格大于给定值，按 seq 升序 */
export function backfillTaskEvents(updatedSinceSeq: number) {
  return get<{ total: number; items: TaskEvent[] }>('/tasks/events/backfill', {
    updated_since_seq: updatedSinceSeq,
  })
}
