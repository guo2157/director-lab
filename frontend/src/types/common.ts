/** 统一错误格式（API.md）：失败响应体含 error.code / error.message / error.detail */
export interface ApiErrorBody {
  error: {
    code: string
    message: string
    detail?: Record<string, unknown> | string | null
  }
}

/** 分页响应（API.md）：items / total / page / page_size */
export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** 分页查询参数：page 默认 1，page_size 默认 20 上限 100 */
export type PaginationParams = {
  page?: number
  page_size?: number
  /** 排序字段白名单，由后端校验 */
  order_by?: string
}
