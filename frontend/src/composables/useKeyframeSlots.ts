import { computed, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { listWorkflows } from '@/api/workflows'
import type { RefSlotInfo } from '@/api/regeneration'

/**
 * 生图（图生图）工作流的参考图能力以工作流声明的 reference_slots 为唯一事实来源，
 * 不在前端写死槽位数量与槽位名：多参考版声明 9 槽，纯文生图版声明 0 槽。
 * 有分镜图时取该资产实际工作流的 available_slots；尚无分镜图时取默认生图工作流的声明。
 */
const IMG_RESERVED_SLOTS = new Set(['ref_image', 'ref_prev_frame', 'first_frame', 'end_frame'])

export function isCharSlot(param: string): boolean {
  return (
    !IMG_RESERVED_SLOTS.has(param) &&
    (param.startsWith('ref_identity') || param.startsWith('ref_character') || param.startsWith('ref_char'))
  )
}

export function isSceneSlot(param: string): boolean {
  return param.startsWith('ref_scene')
}

export function isPropSlot(param: string): boolean {
  return param.startsWith('ref_prop')
}

/** 槽位展示名：角色槽按声明顺序编号，场景/道具按类型命名，未知槽位回落参数名。 */
function imageSlotLabel(param: string, charIndex: number, propIndex: number): string {
  if (isCharSlot(param)) return charIndex === 0 ? '角色身份参考' : `角色参考 ${charIndex + 1}`
  if (isSceneSlot(param)) return '场景基准图'
  if (isPropSlot(param)) return propIndex === 0 ? '道具参考' : `道具参考 ${propIndex + 1}`
  return param
}

/**
 * 归一化工作流参考槽位声明，兼容字符串数组与对象数组。
 *
 * 后端数据库实际保存的是 capabilities.normalize() 产出的 `reference.slots`，
 * 旧形态顶层 `reference_slots` 仅作兼容读取。只读后者会让所有工作流看起来都是 0 槽，
 * 前端槽位面板与图片编号随之全部失效。
 */
function normalizeRefSlots(caps: Record<string, unknown> | null | undefined): RefSlotInfo[] {
  const reference = caps?.reference as Record<string, unknown> | undefined
  // 顺序不能反：顶层 reference_slots 若存在但是空数组，先读它会把已声明槽位判成 0 槽。
  const fromReference = Array.isArray(reference?.slots) ? (reference!.slots as unknown[]) : []
  const fromLegacy = Array.isArray(caps?.reference_slots) ? (caps!.reference_slots as unknown[]) : []
  const raw: unknown[] = fromReference.length ? fromReference : fromLegacy
  const slots: RefSlotInfo[] = []
  for (const item of raw) {
    if (typeof item === 'string') {
      if (item)
        slots.push({
          param: item,
          input_key: null,
          required: false,
          semantic: 'reference',
          role: null,
          system: IMG_RESERVED_SLOTS.has(item),
        })
    } else if (item && typeof item === 'object') {
      const o = item as Record<string, unknown>
      const param = typeof o.param === 'string' ? o.param : ''
      if (!param) continue
      // reference.slots 是全量槽位（H3 为 9 图 + 3 音频），参考图面板只取图片槽，
      // 与后端 declared_reference_slots 只返回图片槽的语义保持一致。
      if (o.media_type && o.media_type !== 'image') continue
      slots.push({
        param,
        input_key: typeof o.input_key === 'string' ? o.input_key : null,
        required: o.optional === false,
        semantic: 'reference',
        role: null,
        system: IMG_RESERVED_SLOTS.has(param),
      })
    }
  }
  return slots
}

/** 参考图槽位：工作流声明 → 槽位元信息（标签 / 输入位置 / 图片编号） */
export function useKeyframeSlots() {
  /** 无分镜图时的槽位来源：复刻后端 _match_workflow 的分镜图选型（默认 → 内置 → 带参考槽位）。 */
  const imageWorkflowsQuery = useQuery({
    queryKey: ['workflows', 'image', 'storyboard'],
    queryFn: () => listWorkflows({ workflow_type: 'image' }),
  })

  const fallbackImageSlots = computed<RefSlotInfo[]>(() => {
    const items = (imageWorkflowsQuery.data.value?.items ?? []) as Array<{
      is_default?: boolean
      is_builtin?: boolean
      capabilities?: Record<string, unknown> | null
    }>
    if (!items.length) return []
    const pick = items.find((w) => w.is_default) ?? items.find((w) => w.is_builtin) ?? items[0]
    const slots = normalizeRefSlots(pick.capabilities)
    // 分镜图会改用带参考槽位的工作流：默认工作流 0 槽时切到声明角色槽位的那一个
    if (!slots.some((s) => !s.system)) {
      for (const w of items) {
        const alt = normalizeRefSlots(w.capabilities)
        if (alt.some((s) => isCharSlot(s.param))) return alt
      }
    }
    return slots
  })

  /** 该资产实际工作流的槽位；为空时回落到默认生图工作流声明。 */
  const kfActualSlots = ref<RefSlotInfo[]>([])
  const kfSlots = computed<RefSlotInfo[]>(() => {
    const actual = kfActualSlots.value.filter((s) => !s.system)
    return actual.length ? actual : fallbackImageSlots.value.filter((s) => !s.system)
  })
  const kfSlotMeta = computed<Record<string, { label: string; inputKey: string }>>(() => {
    const map: Record<string, { label: string; inputKey: string }> = {}
    let charIndex = 0
    let propIndex = 0
    for (const s of kfSlots.value) {
      map[s.param] = {
        label: imageSlotLabel(s.param, isCharSlot(s.param) ? charIndex++ : 0, isPropSlot(s.param) ? propIndex++ : 0),
        inputKey: s.input_key || '—',
      }
    }
    return map
  })
  const kfMaxRefs = computed(() => kfSlots.value.length)
  const kfCharSlots = computed(() => kfSlots.value.filter((s) => isCharSlot(s.param)).map((s) => s.param))
  const kfPropSlots = computed(() => kfSlots.value.filter((s) => isPropSlot(s.param)).map((s) => s.param))
  const kfSceneSlot = computed(() => kfSlots.value.find((s) => isSceneSlot(s.param))?.param ?? '')

  /** 槽位对应的提示词图片编号：images.image_N → N（与工作流 input_key 严格对应） */
  function slotImageNo(slotParam: string): number | null {
    const s = kfSlots.value.find((x) => x.param === slotParam)
    if (!s?.input_key) return null
    const m = String(s.input_key).match(/image_(\d+)\s*$/)
    return m ? Number(m[1]) : null
  }

  return {
    kfActualSlots,
    kfSlots,
    kfSlotMeta,
    kfMaxRefs,
    kfCharSlots,
    kfPropSlots,
    kfSceneSlot,
    slotImageNo,
  }
}

export type KeyframeSlotsApi = ReturnType<typeof useKeyframeSlots>
