import { computed, type ComputedRef, type Ref } from 'vue'
import type { RegenerateRef } from '@/api/regeneration'
import type { Shot } from '@/types/script'
import { isCharSlot, isSceneSlot, isPropSlot, type KeyframeSlotsApi } from './useKeyframeSlots'

export interface KfInjectedRef {
  slot: string
  assetId: string
  filename: string
}

export interface KeyframeRefItem {
  key: string
  slot: string
  name: string
  roleLabel: string
  inputKey: string
  assetId: string
  url: string
  required?: boolean
  /** 真实入参：来自重生成任务记录的实际参考图 */
  injected?: boolean
  /** 对应定妆图 / 基准图尚未生成 */
  assetMissing?: boolean
  /** 当前生图工作流无可用槽位，本次不会传入 */
  noSlot?: boolean
}

interface CharLite {
  id: string
  name: string
  portrait_asset_id?: string | null
}
interface SceneLite {
  id: string
  title: string
  baseline_asset_id?: string | null
}
interface PropLite {
  id: string
  name: string
  anchor_asset_id?: string | null
}

/**
 * 参考图列表：真实入参优先，其余按工作流槽位规则推导并明确标注来源。
 * 首次生成的分镜图没有独立的参考图记录，真实传参保存在任务输入快照的参数里，
 * 形如 { ref_identity: 'ast_xxx_v1.png', ref_scene: 'ast_yyy_v4.png' }，
 * 由调用方从快照还原成 injected 列表传进来。
 */
export function useKeyframeRefList(opts: {
  shot: Ref<Shot | null>
  slots: KeyframeSlotsApi
  refs: Ref<RegenerateRef[]>
  injected: Ref<KfInjectedRef[]>
  chars: Ref<CharLite[]>
  scenes: Ref<SceneLite[]>
  props: Ref<PropLite[]>
}): ComputedRef<KeyframeRefItem[]> {
  return computed<KeyframeRefItem[]>(() => {
    const shot = opts.shot.value
    if (!shot) return []
    const chars = opts.chars.value
    const scenes = opts.scenes.value
    const props = opts.props.value
    const { kfSlotMeta, kfCharSlots, kfPropSlots, kfSceneSlot } = opts.slots

    const items: KeyframeRefItem[] = []
    const seenAsset = new Set<string>()
    const push = (v: KeyframeRefItem) => {
      if (v.assetId) {
        if (seenAsset.has(v.assetId)) return
        seenAsset.add(v.assetId)
      }
      if (items.some((x) => x.key === v.key)) return
      items.push(v)
    }

    const nameOfAsset = (assetId: string, slot: string): string => {
      const c = chars.find((x) => x.portrait_asset_id === assetId)
      if (c) return c.name
      const s = scenes.find((x) => x.baseline_asset_id === assetId)
      if (s) return s.title
      const p = props.find((x) => x.anchor_asset_id === assetId)
      if (p) return p.name
      return kfSlotMeta.value[slot]?.label ?? slot
    }

    // 一（甲）、首次生成的真实传参：从任务输入快照参数还原
    for (const r of opts.injected.value) {
      const meta = kfSlotMeta.value[r.slot]
      const named = nameOfAsset(r.assetId, r.slot)
      push({
        key: `${r.slot}-${r.assetId}`,
        slot: r.slot,
        // 资产已不在项目列表时回落显示文件名，避免只看到槽位名而认不出是哪张图
        name: named === (meta?.label ?? r.slot) ? r.filename : named,
        roleLabel: meta?.label ?? r.slot,
        inputKey: meta?.inputKey ?? '—',
        assetId: r.assetId,
        url: `/api/assets/${r.assetId}/file`,
        required: r.slot === 'ref_identity',
        injected: true,
      })
    }

    // 一（乙）、真实入参：该镜头重生成任务里记录的图排最前
    for (const r of opts.refs.value) {
      if (!r.asset_id) continue
      const slot = r.slot_param
      const meta = kfSlotMeta.value[slot]
      const assetId = r.asset_id as string
      push({
        key: `${slot}-${assetId}`,
        slot,
        name: nameOfAsset(assetId, slot),
        roleLabel: meta?.label ?? r.role ?? slot,
        inputKey: r.input_key || meta?.inputKey || '—',
        assetId,
        url: `/api/assets/${assetId}/file`,
        required: Boolean(r.required),
        injected: true,
      })
    }

    // 提示词中的 @引用（与后端注入顺序一致）
    const promptText = `${shot.image_prompt ?? ''} ${shot.description ?? ''}`
    const atNames = new Set<string>()
    let atIdx = promptText.indexOf('@')
    while (atIdx !== -1) {
      const rest = promptText.slice(atIdx + 1)
      const m = rest.match(/^[^\s，,。.;；:：@]+/)
      if (m) atNames.add(m[0])
      atIdx = promptText.indexOf('@', atIdx + 1)
    }

    // 二、角色：本镜主角色 + @引用角色，按工作流声明的角色槽位顺序依次填充
    const charSlots = kfCharSlots.value
    const relatedChars = chars
      .filter((c) => shot.character_id === c.id || atNames.has(c.name))
      .sort((a, b) => (a.id === shot.character_id ? -1 : b.id === shot.character_id ? 1 : 0))
    relatedChars.forEach((c, idx) => {
      const slot = charSlots[idx] ?? ''
      const assetId = c.portrait_asset_id ?? ''
      push({
        key: `char-${c.id}`,
        slot,
        name: c.name,
        roleLabel: slot ? kfSlotMeta.value[slot].label : '角色参考',
        inputKey: slot ? kfSlotMeta.value[slot].inputKey : '—',
        assetId,
        url: assetId ? `/api/assets/${assetId}/file` : '',
        required: slot === 'ref_identity',
        assetMissing: !assetId,
        noSlot: !slot,
      })
    })

    // 三、场景：所属场景优先占用工作流声明的场景槽位，其余无槽位
    const sceneSlot = kfSceneSlot.value
    const relatedScenes = scenes
      .filter((s) => shot.scene_id === s.id || atNames.has(s.title))
      .sort((a, b) => (a.id === shot.scene_id ? -1 : b.id === shot.scene_id ? 1 : 0))
    relatedScenes.forEach((s, idx) => {
      const slot = idx === 0 ? sceneSlot : ''
      const assetId = s.baseline_asset_id ?? ''
      push({
        key: `scene-${s.id}`,
        slot,
        name: s.title,
        roleLabel: slot ? kfSlotMeta.value[slot].label : '场景参考',
        inputKey: slot ? kfSlotMeta.value[slot].inputKey : '—',
        assetId,
        url: assetId ? `/api/assets/${assetId}/file` : '',
        assetMissing: !assetId,
        noSlot: !slot,
      })
    })

    // 四、道具：按工作流声明的道具槽位顺序填充，未声明的照列但不参与传入
    const propSlots = kfPropSlots.value
    const relatedProps = props.filter((p) => atNames.has(p.name))
    relatedProps.forEach((p, idx) => {
      const slot = propSlots[idx] ?? ''
      const assetId = p.anchor_asset_id ?? ''
      push({
        key: `prop-${p.id}`,
        slot,
        name: p.name,
        roleLabel: slot ? kfSlotMeta.value[slot].label : '道具参考',
        inputKey: slot ? kfSlotMeta.value[slot].inputKey : '—',
        assetId,
        url: assetId ? `/api/assets/${assetId}/file` : '',
        assetMissing: !assetId,
        noSlot: !slot,
      })
    })

    return items
  })
}

export { isCharSlot, isSceneSlot, isPropSlot }
