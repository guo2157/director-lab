import { computed, ref, watch, type ComputedRef } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { listCharacters, listProps } from '@/api/script'
import {
  fetchRegenerateConfig,
  submitRegenerate,
  validateRegenerate,
  type RegenerateRef,
  type ValidateResult,
} from '@/api/regeneration'
import { listAssets, uploadAsset } from '@/api/assets'
import { getWorkflowSchema } from '@/api/workflows'
import type { WorkflowSchema } from '@/types/workflow'
import type { Asset } from '@/types/asset'
import type { Shot } from '@/types/script'
import { useKeyframeSlots, isCharSlot, isSceneSlot, isPropSlot } from './useKeyframeSlots'
import { useKeyframeRefList, type KfInjectedRef } from './useKeyframeRefList'

/** 只依赖 useMessage 的四个方法，避免把 Naive UI 的具体类型带进组合式函数 */
export interface Notify {
  success: (msg: string) => void
  error: (msg: string) => void
  warning: (msg: string) => void
  info: (msg: string) => void
}

interface SceneLite {
  id: string
  title: string
  baseline_asset_id?: string | null
}

/**
 * 分镜图详情 + 重新生成工作台：提示词 + 参考图 + 槽位绑定统一编辑。
 * 详情里的参考图展示、预检、提交、替换上传都在这里，页面只负责打开与刷新。
 */
export function useKeyframeRegen(opts: {
  projectId: ComputedRef<string> | { value: string }
  reviewApproved: ComputedRef<boolean> | { value: boolean }
  scenes: ComputedRef<SceneLite[]> | { value: SceneLite[] }
  message: Notify
  /** 提交成功后：建立轮询基线（由页面提供） */
  onSubmitted: () => Promise<void> | void
  /** 尚未出图的镜头走原生成链路（由页面提供） */
  onFirstGenerate: (shot: Shot, prompt: string) => void
}) {
  const slots = useKeyframeSlots()
  const { kfSlots, kfSlotMeta, kfMaxRefs, kfActualSlots, slotImageNo } = slots

  const detailVisible = ref(false)
  const detailShot = ref<Shot | null>(null)
  const kfRefs = ref<RegenerateRef[]>([])
  const kfInjectedRefs = ref<KfInjectedRef[]>([])
  const kfRefsLoading = ref(false)
  const kfNegativeSupported = ref(false)

  const charsQuery = useQuery({
    queryKey: computed(() => ['characters', opts.projectId.value]),
    queryFn: () => listCharacters(opts.projectId.value),
  })
  const propsQuery = useQuery({
    queryKey: computed(() => ['props', opts.projectId.value]),
    queryFn: () => listProps(opts.projectId.value),
  })

  const chars = computed(
    () => (charsQuery.data.value?.items ?? []) as Array<{ id: string; name: string; portrait_asset_id?: string | null }>,
  )
  const propItems = computed(
    () => (propsQuery.data.value?.items ?? []) as Array<{ id: string; name: string; anchor_asset_id?: string | null }>,
  )
  const scenesLite = computed(() => opts.scenes.value as SceneLite[])

  const kfRefList = useKeyframeRefList({
    shot: detailShot,
    slots,
    refs: kfRefs,
    injected: kfInjectedRefs,
    chars,
    scenes: scenesLite,
    props: propItems,
  })

  /** 打开详情：重置重新生成工作台状态 */
  function openDetail(shot: Shot) {
    detailShot.value = shot
    detailVisible.value = true
    regenPrompt.value = shot.image_prompt ?? ''
    regenMode.value = 'inherit_prompt_only'
    regenRefs.value = []
    regenValidation.value = null
    void loadKeyframeRefs(shot)
  }

  async function loadKeyframeRefs(shot: Shot) {
    kfRefs.value = []
    kfInjectedRefs.value = []
    kfActualSlots.value = []
    kfNegativeSupported.value = false
    if (!shot.current_assets?.keyframe) return
    kfRefsLoading.value = true
    try {
      const cfg = await fetchRegenerateConfig(shot.id, 'keyframe')
      kfActualSlots.value = cfg.available_slots ?? []
      kfRefs.value = (cfg.refs ?? []).filter((r) => r.enabled !== false)
      kfNegativeSupported.value = Boolean(cfg.negative_prompt?.supported)
      // 初始化重新生成工作台：提示词与参考图来自基础版本原始生成配置
      regenPrompt.value = cfg.prompt?.rendered ?? shot.image_prompt ?? ''
      regenMode.value = 'inherit_prompt_only'
      // 默认选中「这个基础版本当时使用的工作流」——用户看到的是真实配置，而不是空白；
      // watch 会顺带拉一次 Schema 并补默认参数（切到第三档即可直接改）
      regenWorkflowId.value = cfg.workflow?.id ?? undefined
      regenParams.value = {}
      regenRefs.value = (cfg.refs ?? []).map((r) => ({ ...r }))
      regenValidation.value = null
      // 快照参数里的 ref_* 键即生成时实际传入的槽位，值为对应资产文件名
      const injected: KfInjectedRef[] = []
      for (const [key, value] of Object.entries(cfg.model_params ?? {})) {
        if (!key.startsWith('ref_') || typeof value !== 'string') continue
        const m = value.match(/(ast_[a-z0-9]+)/i)
        if (!m) continue
        injected.push({ slot: key, assetId: m[1], filename: value })
      }
      kfInjectedRefs.value = injected
    } catch {
      // 配置读取失败时静默降级为槽位推导，不打断详情查看
      kfRefs.value = []
      kfInjectedRefs.value = []
    } finally {
      kfRefsLoading.value = false
    }
  }

  // ── 重新生成工作台 ──
  /** 工作台提示词（来自基础版本原始配置，可编辑） */
  const regenPrompt = ref('')
  /** 工作台模式：默认沿用原配置只改提示词；modify_refs 可增删替换参考图 */
  const regenMode = ref<'inherit_prompt_only' | 'modify_refs' | 'full_reconfigure'>('inherit_prompt_only')

  // 完整重新配置：工作流选择 + Schema 驱动的动态参数（P5）
  const regenWorkflowId = ref<string | undefined>(undefined)
  const workflowSchema = ref<WorkflowSchema | null>(null)
  const regenParams = ref<Record<string, unknown>>({})

  /** 切换工作流时按 Schema 默认值补参数；已填值不覆盖 */
  async function loadWorkflowSchema(id: string | undefined) {
    if (!id) {
      workflowSchema.value = null
      return
    }
    try {
      const schema = await getWorkflowSchema(id)
      workflowSchema.value = schema
      const next: Record<string, unknown> = { ...regenParams.value }
      for (const param of schema.parameters) {
        if (next[param.key] === undefined && param.default !== null && param.default !== undefined) {
          next[param.key] = param.default
        }
      }
      regenParams.value = next
    } catch (e: any) {
      opts.message.error(`读取工作流 Schema 失败：${e?.message ?? e}`)
      workflowSchema.value = null
    }
  }

  watch(regenWorkflowId, (id) => { void loadWorkflowSchema(id) })
  /** 工作台参考图列表（含禁用项；提交时按模式决定是否携带） */
  const regenRefs = ref<RegenerateRef[]>([])
  /** 提交前预检结果（null=未校验） */
  const regenValidation = ref<ValidateResult | null>(null)
  const regenSubmitting = ref(false)
  const regenValidating = ref(false)
  /** 资产选择器状态：为哪个槽位选择/替换资产 */
  const assetPickerVisible = ref(false)
  const assetPickerSlot = ref<string>('')
  const assetPickerKeyword = ref('')
  const assetPickerLoading = ref(false)
  const assetPickerItems = ref<Asset[]>([])
  /** 本地上传状态 */
  const assetUploading = ref(false)
  const regenModeOptions = [
    { label: '沿用原配置，只修改提示词', value: 'inherit_prompt_only' },
    { label: '修改参考图', value: 'modify_refs' },
    { label: '切换工作流 / 改参数', value: 'full_reconfigure' },
  ]
  const regenRefsEditable = computed(() => regenMode.value !== 'inherit_prompt_only')

  /** 工作台参考图展示列表：合并已绑定与可用空槽位，按图片编号排序 */
  const regenRefRows = computed(() => {
    const rows: Array<RegenerateRef & { image_no: number | null; label: string }> = []
    const bound = new Set(regenRefs.value.map((r) => r.slot_param))
    for (const r of regenRefs.value) {
      rows.push({
        ...r,
        image_no: slotImageNo(r.slot_param),
        label: kfSlotMeta.value[r.slot_param]?.label ?? r.slot_param,
      })
    }
    // 未绑定的可用槽位（modify_refs 模式下可新增）
    for (const s of kfSlots.value) {
      if (bound.has(s.param)) continue
      rows.push({
        slot_param: s.param,
        input_key: s.input_key,
        asset_id: null,
        required: s.required,
        semantic: 'reference',
        enabled: false,
        image_no: slotImageNo(s.param),
        label: kfSlotMeta.value[s.param]?.label ?? s.param,
      })
    }
    return rows.sort((a, b) => (a.image_no ?? 99) - (b.image_no ?? 99))
  })

  /** 移除参考图：必需槽位禁止移除；移除后清空校验结果 */
  function removeRegenRef(slotParam: string) {
    const r = regenRefs.value.find((x) => x.slot_param === slotParam)
    if (!r) return
    if (r.required) {
      opts.message.warning(`槽位 ${slotParam} 为当前工作流必需输入，不可移除`)
      return
    }
    regenRefs.value = regenRefs.value.filter((x) => x.slot_param !== slotParam)
    regenValidation.value = null
  }

  /** 打开资产选择器（为指定槽位新增或替换参考图） */
  function openAssetPicker(slotParam: string) {
    assetPickerSlot.value = slotParam
    assetPickerKeyword.value = ''
    assetPickerVisible.value = true
    void loadAssetPickerItems()
  }

  /** 加载资产选择器候选：按槽位用途过滤资产类型 */
  async function loadAssetPickerItems() {
    assetPickerLoading.value = true
    try {
      const slot = kfSlots.value.find((x) => x.param === assetPickerSlot.value)
      const role = slot
        ? isCharSlot(slot.param)
          ? 'character'
          : isSceneSlot(slot.param)
            ? 'scene'
            : isPropSlot(slot.param)
              ? 'prop'
              : null
        : null
      const res = await listAssets({
        project_id: opts.projectId.value,
        asset_type: role ?? undefined,
        keyword: assetPickerKeyword.value || undefined,
        page_size: 50,
      })
      assetPickerItems.value = (res.items ?? []) as Asset[]
    } catch (e: any) {
      opts.message.error(e?.message ?? '加载资产列表失败')
      assetPickerItems.value = []
    } finally {
      assetPickerLoading.value = false
    }
  }

  /** 选中资产：绑定到目标槽位（保留槽位，更新资产） */
  function pickAsset(asset: Asset) {
    const slotParam = assetPickerSlot.value
    if (!slotParam) return
    const existing = regenRefs.value.find((x) => x.slot_param === slotParam)
    const entry: RegenerateRef = {
      slot_param: slotParam,
      input_key: kfSlots.value.find((x) => x.param === slotParam)?.input_key ?? null,
      asset_id: asset.id,
      asset_version: asset.version,
      asset_type: asset.asset_type,
      asset_name: asset.name ?? '',
      required: kfSlots.value.find((x) => x.param === slotParam)?.required ?? false,
      file_path: asset.file_path ?? null,
      semantic: 'reference',
      enabled: true,
    }
    if (existing) Object.assign(existing, entry)
    else regenRefs.value.push(entry)
    assetPickerVisible.value = false
    regenValidation.value = null
  }

  /** 本地上传参考图并绑定到目标槽位 */
  async function uploadRefImage(file: File) {
    const slotParam = assetPickerSlot.value
    if (!slotParam) return
    assetUploading.value = true
    try {
      const asset = await uploadAsset(file, {
        project_id: opts.projectId.value,
        asset_type: 'reference',
        name: file.name.replace(/\.[^.]+$/, ''),
      })
      pickAsset(asset)
      opts.message.success('参考图已上传并绑定')
    } catch (e: any) {
      opts.message.error(e?.message ?? '上传参考图失败')
    } finally {
      assetUploading.value = false
    }
  }

  /** 提交前预检：校验提示词图片引用与参考图槽位一致性 */
  async function runRegenValidation() {
    const shot = detailShot.value
    if (!shot) return
    regenValidating.value = true
    try {
      const payload: Record<string, unknown> = {
        target: 'keyframe',
        mode: regenMode.value,
        prompt: regenPrompt.value,
      }
      if (regenMode.value === 'full_reconfigure') {
        // 只有「切换工作流 / 改参数」才允许覆盖工作流与参数；
        // 其余模式沿用原快照，避免静默改变历史配置
        if (regenWorkflowId.value) payload.workflow_id = regenWorkflowId.value
        payload.params = { ...regenParams.value }
      }
      if (regenMode.value !== 'inherit_prompt_only') {
        payload.refs = regenRefs.value.map((r) => ({
          slot_param: r.slot_param,
          asset_id: r.asset_id,
          enabled: r.enabled !== false,
        }))
      }
      regenValidation.value = await validateRegenerate(shot.id, payload)
    } catch (e: any) {
      opts.message.error(e?.message ?? '校验失败')
    } finally {
      regenValidating.value = false
    }
  }

  /** 提交重新生成：先预检（错误阻断），再经 workbench API 提交，完成后直接替换当前分镜图 */
  async function submitRegen() {
    const shot = detailShot.value
    if (!shot) return
    if (!opts.reviewApproved.value) {
      opts.message.warning('公共资产尚未通过人工审核，暂不可生成分镜图')
      return
    }
    if (shot.current_assets?.keyframe?.locked) {
      opts.message.warning('该分镜图已锁定，请先解锁')
      return
    }
    regenSubmitting.value = true
    try {
      // 1. 提交前预检：错误时阻断
      const payload: Record<string, unknown> = {
        target: 'keyframe',
        mode: regenMode.value,
        prompt: regenPrompt.value,
        idempotency_key: `${shot.id}-keyframe-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
      }
      if (regenMode.value !== 'inherit_prompt_only') {
        payload.refs = regenRefs.value.map((r) => ({
          slot_param: r.slot_param,
          asset_id: r.asset_id,
          enabled: r.enabled !== false,
        }))
      }
      const check = await validateRegenerate(shot.id, payload)
      regenValidation.value = check
      if (check.errors.length) {
        opts.message.error('校验未通过，请修正后重试')
        return
      }
      // 2. 提交 workbench 任务（产物直接作为当前分镜图，不保留候选版本）
      const job = await submitRegenerate(shot.id, payload)
      opts.message.success(`已提交重新生成任务（任务 ${job.id}），生成完成后自动替换当前分镜图`)
      detailVisible.value = false
      await opts.onSubmitted()
    } catch (e: any) {
      opts.message.error(e?.message ?? '提交重新生成任务失败')
    } finally {
      regenSubmitting.value = false
    }
  }

  /** 详情页生成入口：已有分镜图走工作台重新生成；首次生成同步工作台提示词后走原链路 */
  function detailGenerate(shot: Shot) {
    if (shot.current_assets?.keyframe) {
      void submitRegen()
      return
    }
    opts.onFirstGenerate(shot, regenPrompt.value)
  }

  return {
    // 弹窗
    detailVisible,
    detailShot,
    openDetail,
    // 槽位
    kfSlots,
    kfSlotMeta,
    kfMaxRefs,
    kfRefs,
    kfRefsLoading,
    kfNegativeSupported,
    kfRefList,
    // 工作台
    regenPrompt,
    regenMode,
    regenWorkflowId,
    workflowSchema,
    regenParams,
    regenModeOptions,
    regenRefs,
    regenRefRows,
    regenRefsEditable,
    regenValidation,
    regenSubmitting,
    regenValidating,
    removeRegenRef,
    runRegenValidation,
    submitRegen,
    detailGenerate,
    // 资产选择器
    assetPickerVisible,
    assetPickerSlot,
    assetPickerKeyword,
    assetPickerLoading,
    assetPickerItems,
    assetUploading,
    openAssetPicker,
    loadAssetPickerItems,
    pickAsset,
    uploadRefImage,
  }
}