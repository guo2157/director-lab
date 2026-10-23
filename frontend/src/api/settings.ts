import { get, post, put } from './client'
import type { AppSettings, Channel, HealthStatus, ClearCacheResult } from '@/types/compose'

/** 读取全局设置（密钥掩码 + api_key_set） */
export function getSettings() {
  return get<AppSettings>('/settings')
}

/** 更新全局设置（密钥传空字符串=清除） */
export function updateSettings(payload: AppSettings) {
  return put<AppSettings>('/settings', payload)
}

/** 测试 ComfyUI 连接（失败 detail.reason 枚举 connection_refused/timeout/unauthorized/invalid_response/dns_error） */
export function testComfyui() {
  return post<{ ok: boolean; reason?: string; message?: string }>('/settings/test-comfyui')
}

/** 测试语言模型连接 */
export function testLlm() {
  return post<{ ok: boolean; reason?: string; message?: string }>('/settings/test-llm')
}

/** 三方通道列表 */
export function listChannels() {
  return get<Channel[]>('/settings/channels')
}

/** 更新三方通道（priority 启用通道内唯一，冲突 400） */
export function updateChannels(channels: Channel[]) {
  return put<Channel[]>('/settings/channels', { channels })
}

/** 测试三方通道连接 */
export function testChannel(channelId: string) {
  return post<{ ok: boolean; reason?: string; message?: string }>(`/settings/channels/${channelId}/test`)
}

/** 备份设置（并发触发返回 409） */
export function backupSettings() {
  return post<{ ok: boolean; file_path?: string }>('/settings/backup')
}

/** 清理上传缓存（60 秒频控，重复调用返回上次结果 + cached:true） */
export function clearUploadCache() {
  return post<ClearCacheResult>('/settings/clear-upload-cache')
}

/** 健康检查（status/database/comfyui/worker/disk） */
export function getHealth() {
  return get<HealthStatus>('/health')
}
