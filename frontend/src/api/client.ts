import axios, { AxiosError } from 'axios'
import type { ApiErrorBody } from '@/types/common'

/**
 * 统一 API 错误（与 API.md 错误格式一致：error.code / error.message / error.detail）
 */
export class ApiError extends Error {
  /** HTTP 状态码；网络错误/超时等无响应时为 0 */
  status: number
  /** 后端错误码，如 project_not_found、asset_locked；无响应时为 network_error / timeout */
  code: string
  /** 错误详情（结构化或字符串） */
  detail?: Record<string, unknown> | string | null
  /** 是否为可重试错误（网络错误/超时/502/503/504） */
  retryable: boolean

  constructor(status: number, code: string, message: string, detail?: Record<string, unknown> | string | null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
    this.retryable = status === 0 || [502, 503, 504].includes(status)
  }

  /** 是否为指定错误码 */
  is(code: string): boolean {
    return this.code === code
  }
}

/** 从 AxiosError 提取统一错误 */
function toApiError(err: AxiosError<ApiErrorBody>): ApiError {
  if (err.response) {
    const body = err.response.data
    const errorBody = body && typeof body === 'object' && 'error' in body ? body.error : undefined
    if (errorBody?.message) {
      return new ApiError(
        err.response.status,
        errorBody.code ?? `http_${err.response.status}`,
        errorBody.message,
        errorBody.detail ?? null,
      )
    }
    // 后端统一错误格式：{"detail": {"code": "...", "message": "...", ...}}
    const detailBody =
      body && typeof body === 'object' && 'detail' in body &&
      typeof (body as { detail: unknown }).detail === 'object' && (body as { detail: Record<string, unknown> }).detail !== null
        ? (body as { detail: Record<string, unknown> }).detail
        : undefined
    if (detailBody && typeof detailBody.message === 'string') {
      return new ApiError(
        err.response.status,
        typeof detailBody.code === 'string' ? detailBody.code : `http_${err.response.status}`,
        detailBody.message,
        detailBody,
      )
    }
    // FastAPI 请求校验错误（422）：{"detail": [{loc, msg, type}, ...]}，转为可读信息
    if (
      err.response.status === 422 &&
      body && typeof body === 'object' && 'detail' in body &&
      Array.isArray((body as { detail: unknown }).detail)
    ) {
      const items = (body as { detail: Array<{ loc?: unknown[]; msg?: string }> }).detail
      const msg = items
        .map((d) => {
          const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : ''
          return field ? `${field}: ${d.msg ?? '校验失败'}` : (d.msg ?? '校验失败')
        })
        .join('；')
      return new ApiError(422, 'validation_error', `请求参数校验失败：${msg}`, null)
    }
    return new ApiError(
      err.response.status,
      `http_${err.response.status}`,
      err.message,
      null,
    )
  }
  if (err.code === 'ECONNABORTED') {
    return new ApiError(0, 'timeout', '请求超时，请稍后重试')
  }
  return new ApiError(0, 'network_error', '网络错误，请检查连接后重试')
}

/**
 * axios 实例：baseURL /api，统一错误处理
 * 本期内网不鉴权，预留 Authorization: Bearer <token>
 */
export const apiClient = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

apiClient.interceptors.request.use((config) => {
  // 预留鉴权头（本期内网不鉴权）
  // const token = ...; if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorBody>) => Promise.reject(toApiError(error)),
)

/** GET 请求 */
export async function get<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const res = await apiClient.get<T>(url, { params })
  return res.data
}

/** POST 请求 */
export async function post<T>(url: string, data?: unknown): Promise<T> {
  const res = await apiClient.post<T>(url, data)
  return res.data
}

/** PATCH 请求 */
export async function patch<T>(url: string, data?: unknown): Promise<T> {
  const res = await apiClient.patch<T>(url, data)
  return res.data
}

/** PUT 请求 */
export async function put<T>(url: string, data?: unknown): Promise<T> {
  const res = await apiClient.put<T>(url, data)
  return res.data
}

/** DELETE 请求 */
export async function del<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const res = await apiClient.delete<T>(url, { params })
  return res.data
}
