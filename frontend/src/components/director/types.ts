/** 导演台上下文栏共用的展示类型（只读投影，不含请求逻辑） */

/** 侧栏用的项目简要信息（来自 GET /projects） */
export interface ProjectBrief {
  id: string
  name: string
  status: string
  current_stage: string
  shot_total: number
  shot_done: number
}
