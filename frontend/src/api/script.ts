import { get, post, patch, put, del } from './client'
import type { Paginated, PaginationParams } from '@/types/common'
import type { Scene, Shot, Character, Prop, ProjectShotItem, ShotKind } from '@/types/script'

/** 后端兼容归一化：接口可能返回纯数组或分页结构，统一为 Paginated */
function normalizeList<T>(res: unknown): Paginated<T> {
  if (Array.isArray(res)) return { items: res as T[], total: res.length, page: 1, page_size: res.length }
  return res as Paginated<T>
}
/** 场景列表（episode 筛选依据 scene_episode 关联表；镜头数 >100 的项目必须 include_shots=false 分批加载） */
export async function listScenes(
  projectId: string,
  params?: PaginationParams & { episode?: number; include_shots?: boolean },
) {
  return normalizeList<Scene>(await get<unknown>(`/projects/${projectId}/scenes`, params))
}

/** 场景详情 */
export function getScene(id: string) {
  return get<Scene>(`/scenes/${id}`)
}

/** 新建场景 */
export function createScene(payload: { project_id: string; title: string; summary?: string; episodes?: number[] }) {
  return post<Scene>('/scenes', payload)
}

/** 更新场景 */
export function updateScene(id: string, payload: { title?: string; summary?: string }) {
  return patch<Scene>(`/scenes/${id}`, payload)
}

/** 删除场景 */
export async function deleteScene(id: string) {
  await del<void>(`/scenes/${id}`)
}

/** 场景追加集数（重复添加同集 409；响应含 requires_parse 布尔） */
export function addSceneEpisode(sceneId: string, episode: number) {
  return post<Scene & { requires_parse: boolean }>(`/scenes/${sceneId}/episodes`, { episode })
}

/** 场景移除集数（移除最后一集拒绝；该集有镜头返回 409 asset_in_use） */
export function removeSceneEpisode(sceneId: string, episode: number) {
  return del<Scene>(`/scenes/${sceneId}/episodes/${episode}`)
}

/** 场景内镜头列表 */
export function listSceneShots(sceneId: string, params?: PaginationParams & { episode?: number }) {
  return get<Paginated<Shot>>(`/scenes/${sceneId}/shots`, params)
}

/** 场景内镜头排序（仅同场景同集内排序，含其他场景/集 ID 返回 400） */
export function reorderSceneShots(sceneId: string, shotIds: string[], episode?: number) {
  return put<Scene>(`/scenes/${sceneId}/shots/order`, { shot_ids: shotIds, episode })
}

/** 项目镜头列表（episode 必填，按 scene_episode.seq_in_episode + 镜头 seq 升序） */
export function listProjectShots(projectId: string, params: PaginationParams & { episode: number }) {
  return get<Paginated<ProjectShotItem>>(`/projects/${projectId}/shots`, params)
}

/** 镜头详情（含 consistency 与聚合素材） */
export function getShot(id: string) {
  return get<Shot>(`/shots/${id}`)
}

/** 确认分镜图（人工审核通过，置 keyframe_confirmed=true，放行该镜视频生成） */
export function confirmShotKeyframe(shotId: string) {
  return post<{ id: string; keyframe_confirmed: boolean }>(`/shots/${shotId}/keyframe/confirm`)
}

/** 取消分镜图确认（重绘或重新审核时回退） */
export function unconfirmShotKeyframe(shotId: string) {
  return post<{ id: string; keyframe_confirmed: boolean }>(`/shots/${shotId}/keyframe/unconfirm`)
}

/** 新建镜头（dialogue/narration 时 dialogue 必填；(scene_id, episode) 须存在于 scene_episode，否则 422） */
export function createShot(payload: {
  scene_id: string
  episode: number
  seq?: number
  shot_kind: ShotKind
  title?: string
  description?: string
  dialogue?: string
  image_prompt?: string
  video_prompt?: string
  duration?: number
  character_id?: string
  speaker_id?: string
}) {
  return post<Shot>('/shots', payload)
}

/** 更新镜头（按依赖矩阵标 stale 不自动触发任务；duration 超出 4–15 秒返回 warning duration_out_of_range 但仍接受） */
export function updateShot(id: string, payload: Partial<Parameters<typeof createShot>[0]>) {
  return patch<Shot & { warnings?: string[] }>(`/shots/${id}`, payload)
}

/** 删除镜头 */
export async function deleteShot(id: string) {
  await del<void>(`/shots/${id}`)
}

/** 角色列表 */
export async function listCharacters(projectId: string, params?: PaginationParams) {
  return normalizeList<Character>(await get<unknown>(`/projects/${projectId}/characters`, params))
}

/** 新建角色（name 项目内唯一，重复 409） */
export function createCharacter(payload: { project_id: string; name: string; description?: string }) {
  return post<Character>('/characters', payload)
}

/** 更新角色（改名同步改写 shot.image_prompt/video_prompt 中 @旧名） */
export function updateCharacter(id: string, payload: { name?: string; description?: string }) {
  return patch<Character>(`/characters/${id}`, payload)
}

/** 删除角色（被引用返回 409 asset_in_use） */
export async function deleteCharacter(id: string) {
  await del<void>(`/characters/${id}`)
}

/** 合并角色（source=target 返回 400；定妆图处置见 API.md） */
export function mergeCharacters(sourceId: string, targetId: string) {
  return post<Character>('/characters/merge', { source_id: sourceId, target_id: targetId })
}

/** 道具列表 */
export async function listProps(projectId: string, params?: PaginationParams) {
  return normalizeList<Prop>(await get<unknown>(`/projects/${projectId}/props`, params))
}

/** 新建道具 */
export function createProp(payload: { project_id: string; name: string; description?: string }) {
  return post<Prop>('/props', payload)
}

/** 更新道具（改名同步改写提示词中 @旧名） */
export function updateProp(id: string, payload: { name?: string; description?: string }) {
  return patch<Prop>(`/props/${id}`, payload)
}

/** 删除道具（被引用返回 409 asset_in_use） */
export async function deleteProp(id: string) {
  await del<void>(`/props/${id}`)
}
