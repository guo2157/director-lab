import { get, post, put } from './client'
import type { AppSettings, ComfyUIInstance, ComfyUIInstanceTestResult, HealthStatus, ClearCacheResult } from '@/types/compose'

/** 读取全局设置（密钥掩码 + api_key_set） */
export function getSettings() {
  return get<AppSettings>('/settings')
}

/** 更新全局设置（密钥传空字符串=清除） */
export function updateSettings(payload: AppSettings) {
  return put<AppSettings>('/settings', payload)
}

/** 测试 ComfyUI 连接（可指定实例地址；失败 detail.reason 枚举 connection_refused/timeout/unauthorized/invalid_response/dns_error） */
export function testComfyui(baseUrl?: string) {
  return post<{ ok: boolean; reason?: string; message?: string; latency_ms?: number }>('/settings/test-comfyui', baseUrl ? { base_url: baseUrl } : undefined)
}

/** 测试语言模型连接 */
export function testLlm() {
  return post<{ ok: boolean; reason?: string; message?: string }>('/settings/test-llm')
}

/** 查询语言模型列表（OpenAI 兼容 /models 接口，供模型名称下拉选择） */
export function listLlmModels(payload?: { base_url?: string; api_key?: string }) {
  return post<{ ok: boolean; models: string[] }>('/settings/llm-models', payload)
}

/** ComfyUI 实例列表（多实例加权负载均衡） */
export function listComfyuiInstances() {
  return get<ComfyUIInstance[]>('/settings/comfyui-instances')
}

/** 更新 ComfyUI 实例列表 */
export function updateComfyuiInstances(instances: ComfyUIInstance[]) {
  return put<ComfyUIInstance[]>('/settings/comfyui-instances', instances)
}

/** 逐实例测试连通性 */
export function testComfyuiInstances() {
  return post<{ ok: boolean; instances: ComfyUIInstanceTestResult[] }>('/settings/comfyui-instances/test')
}

/** 备份设置（并发触发返回 409） */
export function backupSettings() {
  return post<{ ok: boolean; file_path?: string }>('/settings/backup')
}

/** 清理上传缓存（60 秒频控，重复调用返回上次结果 + cached:true） */
export function clearUploadCache() {
  return post<ClearCacheResult>('/settings/clear-upload-cache')
}

/** 提示词列表（D-15：name/externalized/sha256_8/length） */
export interface PromptInfo {
  name: string
  externalized: boolean
  sha256_8: string
  length: number
}

/** 列出全部提示词 */
export function listPrompts() {
  return get<PromptInfo[]>('/settings/prompts')
}

/** 读取提示词内容（外置文件优先，缺失回退内置默认值） */
export function getPrompt(name: string) {
  return get<{ name: string; content: string }>(`/settings/prompts/${name}`)
}

/** 写入提示词（保存即生效；写入前自动备份最多 5 份） */
export function writePrompt(name: string, content: string) {
  return put<{ name: string; written: boolean; length: number }>(`/settings/prompts/${name}`, { content })
}

/** 重置提示词为内置默认值（删除外置文件） */
export function resetPrompt(name: string) {
  return post<{ name: string; reset: boolean }>(`/settings/prompts/${name}/reset`)
}

/** 健康检查（status/database/comfyui/worker/disk） */
export function getHealth() {
  return get<HealthStatus>('/health')
}
