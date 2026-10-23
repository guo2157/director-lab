import { get, post, patch, del } from './client'
import { apiClient } from './client'
import type { Paginated, PaginationParams } from '@/types/common'
import type { Workflow, WorkflowType } from '@/types/asset'

/** 工作流列表（workflow_type 筛选；工作流切换时传 workflow_id 覆盖自动匹配） */
export function listWorkflows(params?: PaginationParams & { workflow_type?: WorkflowType }) {
  return get<Paginated<Workflow>>('/workflows', params)
}

/** 上传工作流（multipart，file ≤10MB，返回 parsed_schema.params/outputs） */
export function uploadWorkflow(file: File, payload: { name: string; workflow_type: WorkflowType; is_default?: boolean }) {
  const form = new FormData()
  form.append('file', file)
  form.append('name', payload.name)
  form.append('workflow_type', payload.workflow_type)
  if (payload.is_default !== undefined) form.append('is_default', String(payload.is_default))
  return apiClient.post<Workflow>('/workflows', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 60000,
  }).then((res) => res.data)
}

/** 更新工作流（生成新不可变版本；设默认自动取消同类型其他默认） */
export function updateWorkflow(id: string, payload: { name?: string; is_default?: boolean; file?: File }) {
  if (payload.file) {
    const form = new FormData()
    form.append('file', payload.file)
    if (payload.name) form.append('name', payload.name)
    if (payload.is_default !== undefined) form.append('is_default', String(payload.is_default))
    return apiClient.patch<Workflow>(`/workflows/${id}`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
    }).then((res) => res.data)
  }
  return patch<Workflow>(`/workflows/${id}`, { name: payload.name, is_default: payload.is_default })
}

/** 校验工作流（前端先本地解析再后端二次校验） */
export function validateWorkflow(id: string) {
  return post<{ valid: boolean; errors?: string[] }>(`/workflows/${id}/validate`)
}

/** 删除工作流（被引用返回 409 workflow_in_use） */
export async function deleteWorkflow(id: string) {
  await del<void>(`/workflows/${id}`)
}
