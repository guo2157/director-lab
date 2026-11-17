/**
 * 镜头详情（分镜视频）的状态与动作编排。
 *
 * 这里只做「状态 + 动作」：接口调用、idempotency_key、失效传播口径与重构前
 * StoryboardView 内的实现完全一致，布局层（ShotDetailModal / 三个标签页）
 * 不再持有业务逻辑。
 */
import { computed, ref } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useMessage } from 'naive-ui'
import { updateShot } from '@/api/script'
import { generateShot } from '@/api/storyboard'
import {
  fetchRegenerateConfig,
  fetchRegenerateWorkspace,
  saveRegenerateWorkspace,
  submitRegenerateWorkspace,
  type WorkspaceReference,
  type WorkspaceSlot,
} from '@/api/regeneration'
import type { Shot } from '@/types/script'
import type { GenerateShotPayload } from '@/types/job'

/** 视频生成档位：对应库里三套 4 步工作流 */
export type ClipTier = 'fast_5s' | 'std_5s' | 'quality_10s'

/**
 * 系统级默认负向提示词：统一维护，用户未自定义时作为默认值；
 * 图文可共用，是否启用按模型能力决定。
 */
export const DEFAULT_NEGATIVE_PROMPT = [
  'low quality, worst quality, low resolution, blurry, out of focus, pixelated, noisy',
  'compression artifacts, excessive motion blur, overexposed, underexposed, poor lighting',
  'unnatural colors, oversaturated, washed-out colors, excessive sharpening',
  'distorted image, visual artifacts, malformed anatomy, deformed body, unnatural body proportions',
  'deformed face, asymmetrical facial features, malformed hands, malformed feet',
  'extra fingers, missing fingers, extra limbs, missing limbs, duplicated body parts',
  'unnatural pose, unnatural movement, stiff motion, jerky motion, inconsistent motion',
  'temporal inconsistency, flickering, jitter, ghosting, double images, frame instability',
  'warped objects, distorted geometry, inconsistent perspective, inconsistent lighting, inconsistent shadows',
  'floating objects, duplicated objects, disappearing objects, unwanted objects',
  'unexpected background changes, inconsistent character appearance, identity drift',
  'inconsistent facial features, inconsistent clothing, inconsistent hairstyle, inconsistent object shapes',
  'unnatural physical interactions, broken contact between objects, clipping, penetration artifacts',
  'text artifacts, random text, watermark, logo, unwanted borders, unwanted black bars',
].join(', ')

/** 参考图绑定的可选项来源（角色定妆图 / 场景基准图 / 道具锚定图） */
interface AssetOptionSource {
  scenes: () => Array<{ id: string; title: string; baseline_asset_id?: string | null }>
  characters: () => Array<{ id: string; name: string; portrait_asset_id?: string | null }>
  props: () => Array<{ name: string; anchor_asset_id?: string | null }>
}

export interface ShotDetailDeps extends AssetOptionSource {
  projectId: () => string
  /** 当前镜头（由页面按 shots 列表解析后传入，切换镜头即变化） */
  shot: () => Shot | null
  readinessAll: () => boolean
  clipWorkflowId: () => string | undefined
  clipTier: () => ClipTier
  setClipTier: (tier: ClipTier) => void
  /** 该镜头是否已有在途 generate_clip 任务（决定按钮是「生成视频」还是「重新生成」） */
  clipJobActive: () => boolean
  /** 提交成功：页面刷新场景/任务列表并启动进度轮询 */
  onSubmitted: () => void
  /** 提示词保存后需要重新拉取镜头（后端会做失效传播） */
  onPromptsSaved: (shot: Shot) => void | Promise<void>
  /** 提交成功后关闭详情 */
  onClose: () => void
}

export function useShotDetail(deps: ShotDetailDeps) {
  const message = useMessage()
  const queryClient = useQueryClient()

  const shot = computed(() => deps.shot())
  const projectId = computed(() => deps.projectId())

  // --- 提示词 ---
  const videoPrompt = ref('')
  /** 负向提示词：默认用统一模板，用户可改；工作流未声明支持时不显示也不传参 */
  const negativePrompt = ref(DEFAULT_NEGATIVE_PROMPT)
  /** 当前视频工作流是否支持负向提示词，由重生成配置接口的 negative_prompt.supported 决定 */
  const negativeSupported = ref(false)
  /** 打开详情时的原始视频提示词：用于判断用户是否真的改过 */
  const originVideoPrompt = ref('')
  const savingPrompts = ref(false)

  // --- 参考图绑定 ---
  const refBindingsLoading = ref(false)
  const refBindingsSaving = ref(false)
  const bindings = ref<WorkspaceReference[]>([])
  const slots = ref<WorkspaceSlot[]>([])
  const chainFrameStatus = ref<'ready' | 'missing' | 'not_applicable' | null>(null)

  // --- 生成 ---
  const generating = ref(false)
  const directRegenLoading = ref(false)

  /**
   * 按 MiniMax H3 提示词规范（I2VA：首帧关键帧全参考）构建默认视频提示词。
   * <Picture 1> 为 H3 图片引用标签；@角色名/@场景名 为项目注入语法，
   * 后端据此把定妆图/基准图文件注入生成入参 __media__。
   */
  function buildH3VideoPrompt(target: Shot): string {
    const scene = deps.scenes().find((s) => s.id === target.scene_id)
    const ch = deps.characters().find((c) => c.id === target.character_id)
    const refs: string[] = []
    if (ch) refs.push(`@${ch.name}`)
    if (scene) refs.push(`@${scene.title}`)
    const refText = refs.length ? `${refs.join(' ')} ` : ''
    const desc =
      target.description || target.dialogue || '（补充画面描述：主体、动作、环境、镜头运动）'
    return [
      'For the target video, at 0.00 seconds into the target video, <Picture 1> is fully referenced.',
      '',
      `integrated_multimodal_description: [Shot 1] Live-action, cinematic. ${refText}${desc} <Picture 1> defines the style, subjects and composition at 0.00 seconds; identity, clothing, colors and spatial relationships remain consistent throughout the ${target.duration}-second video.`,
      'overall_soundscape: Ambient sound of the scene plus physical action sounds synchronized with the movement; non-verbal human sounds where appropriate.',
      'non_diegetic_music: (描述背景音乐情绪、节奏与乐器；无则填 None.)',
    ].join('\n')
  }

  /** 负向提示词支持检测：镜头已有产物时从生成配置接口获取 */
  async function loadNegativeSupported(target: Shot) {
    negativeSupported.value = false
    const hasOutput = Boolean(target.current_assets?.clip || target.current_assets?.keyframe)
    if (!hasOutput) return
    try {
      const cfg = await fetchRegenerateConfig(target.id, 'clip')
      negativeSupported.value = Boolean(cfg.negative_prompt?.supported)
    } catch {
      negativeSupported.value = false
    }
  }

  async function loadRefBindings(shotId: string) {
    refBindingsLoading.value = true
    try {
      const ws = await fetchRegenerateWorkspace(shotId, 'clip')
      bindings.value = ws.references ?? []
      chainFrameStatus.value = ws.chain_frame?.status ?? null
      // 档位跟随该镜头实际使用的工作流，避免打开详情后被默认档位覆盖
      const wfName = ws.workflow?.name ?? ''
      const tier = (['fast_5s', 'std_5s', 'quality_10s'] as const).find((t) => wfName.endsWith(t))
      if (tier) deps.setClipTier(tier)
      slots.value = (ws.available_slots ?? []).map((s) => ({
        param: s.param,
        node_id: s.node_id,
        input_key: s.input_key,
        image_no: s.image_no,
        required: s.required,
      }))
    } catch {
      bindings.value = []
      slots.value = []
      chainFrameStatus.value = null
    } finally {
      refBindingsLoading.value = false
    }
  }

  /** 打开/切换镜头：重置提示词并拉取参考图绑定 */
  async function open(target: Shot) {
    videoPrompt.value = target.video_prompt || buildH3VideoPrompt(target)
    // 负向提示词无独立存储字段，每次打开从系统默认模板起
    negativePrompt.value = DEFAULT_NEGATIVE_PROMPT
    // 以实际展示的内容作为比对基准：自动填充的默认提示词不算用户改动
    originVideoPrompt.value = videoPrompt.value
    await Promise.all([loadNegativeSupported(target), loadRefBindings(target.id)])
  }

  const unboundSlots = computed(() => {
    const bound = new Set(bindings.value.map((r) => r.slot_id))
    return slots.value.filter((s) => !bound.has(s.param))
  })

  function addBinding(slotParam: string) {
    const slot = slots.value.find((s) => s.param === slotParam)
    if (!slot) return
    bindings.value = [
      ...bindings.value,
      {
        reference_id: `ref_${slotParam}_${Date.now()}`,
        slot_id: slotParam,
        node_id: slot.node_id,
        input_key: slot.input_key,
        image_no: slot.image_no ?? null,
        asset_id: null,
        enabled: true,
        required: Boolean(slot.required),
        status: 'pending_confirmation',
      } as WorkspaceReference,
    ]
  }

  function removeBinding(referenceId: string) {
    const ref = bindings.value.find((r) => r.reference_id === referenceId)
    if (ref?.required) {
      message.warning('该槽位为工作流必需输入，不可移除，只能替换资产或关闭启用开关')
      return
    }
    bindings.value = bindings.value.filter((r) => r.reference_id !== referenceId)
  }

  function setBindingAsset(referenceId: string, assetId: string | null) {
    const ref = bindings.value.find((r) => r.reference_id === referenceId)
    if (ref) ref.asset_id = assetId
  }

  function setBindingEnabled(referenceId: string, enabled: boolean) {
    const ref = bindings.value.find((r) => r.reference_id === referenceId)
    if (ref) ref.enabled = enabled
  }

  const assetOptions = computed(() => {
    const opts: { label: string; value: string }[] = []
    for (const c of deps.characters()) {
      if (c.portrait_asset_id) opts.push({ label: `${c.name}（定妆）`, value: c.portrait_asset_id })
    }
    for (const s of deps.scenes()) {
      if (s.baseline_asset_id) opts.push({ label: `${s.title}（场景）`, value: s.baseline_asset_id })
    }
    for (const p of deps.props()) {
      if (p.anchor_asset_id) opts.push({ label: `${p.name}（道具）`, value: p.anchor_asset_id })
    }
    const keyframeId = shot.value?.current_assets?.keyframe?.asset_id
    if (keyframeId) opts.push({ label: '本镜分镜图', value: keyframeId })
    // 已绑定但不在上述选项里的资产（如上一镜尾帧）也要有可读名称
    for (const ref of bindings.value) {
      if (ref.asset_id && ref.asset_name && !opts.some((o) => o.value === ref.asset_id)) {
        opts.push({ label: ref.asset_name, value: ref.asset_id })
      }
    }
    return opts
  })

  async function saveRefBindings(): Promise<boolean> {
    const target = shot.value
    if (!target) return false
    refBindingsSaving.value = true
    try {
      await saveRegenerateWorkspace(target.id, {
        target: 'clip',
        references: bindings.value,
        character_bindings: [],
        spatial_layout: { characters: [] },
        action_timeline: { actions: [] },
        prop_bindings: [],
        prompt_sections: {},
        final_prompt: '',
        parameters: {},
      })
      message.success('参考图绑定已保存')
      await loadRefBindings(target.id)
      return true
    } catch (e: unknown) {
      message.error(`保存失败：${(e as Error)?.message ?? e}`)
      return false
    } finally {
      refBindingsSaving.value = false
    }
  }

  /**
   * 保存提示词。
   * 后端在提示词变化时会做失效传播，因此只提交真正改过的字段，未改动直接返回。
   */
  async function savePrompts(): Promise<boolean> {
    const target = shot.value
    if (!target) return false
    const videoChanged = videoPrompt.value !== originVideoPrompt.value
    if (!videoChanged) {
      message.info('提示词没有改动')
      return false
    }
    const hasClip = Boolean(target.current_assets?.clip)
    if (hasClip) {
      message.warning('视频提示词已修改，原有视频会被标记为过期，需要重新生成。')
    }
    savingPrompts.value = true
    try {
      await updateShot(target.id, { video_prompt: videoPrompt.value })
      target.video_prompt = videoPrompt.value
      originVideoPrompt.value = videoPrompt.value
      message.success('提示词已保存')
      if (hasClip) {
        await deps.onPromptsSaved(target)
      }
      return true
    } catch (e: unknown) {
      message.error(`保存失败: ${(e as Error)?.message ?? e}`)
      return false
    } finally {
      savingPrompts.value = false
    }
  }

  /** 提交生成（分镜图 / 视频），携带详情页编辑后的提示词 */
  async function generateFromDrawer(kind: 'keyframe' | 'clip') {
    const target = shot.value
    if (!target) return
    const params: Record<string, string> = {}
    if (kind === 'clip') {
      if (videoPrompt.value) params.prompt = videoPrompt.value
      // 负向提示词仅在当前工作流声明支持时传入
      if (negativeSupported.value && negativePrompt.value.trim()) {
        params.negative_prompt = negativePrompt.value.trim()
      }
    }
    generating.value = true
    try {
      await generateShot(target.id, {
        target: kind,
        params,
        workflow_id: kind === 'clip' ? deps.clipWorkflowId() : undefined,
      } as GenerateShotPayload)
      message.success('生成任务已提交，正在生成（进度见顶部进度面板）')
      queryClient.invalidateQueries({ queryKey: ['scenes', projectId.value] })
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId.value, 'generate_clip'] })
      deps.onSubmitted()
    } catch (e: unknown) {
      message.error(`生成失败: ${(e as Error)?.message ?? e}`)
    } finally {
      generating.value = false
    }
  }

  /** 该镜头是否已有视频产物（含正在生成的在途任务）：有则可视为『重新生成』 */
  const clipActionIsRegenerate = computed(() => {
    const target = shot.value
    if (!target) return false
    if (target.current_assets?.clip) return true
    return deps.clipJobActive()
  })
  const clipActionLabel = computed(() => (clipActionIsRegenerate.value ? '重新生成' : '生成视频'))

  /**
   * 重新生成视频：不弹工作台，直接复用详情页当前的 H3 提示词与参考图绑定提交。
   * 生成成功后由执行器把新视频置为 current，原视频转历史版本。
   */
  async function regenerateClipDirect() {
    const target = shot.value
    if (!target) return
    directRegenLoading.value = true
    try {
      // 只提交已绑定资产的槽位；手动关闭的槽位保留 enabled=false
      const references = bindings.value
        .filter((r) => r.asset_id)
        .map((r) => ({
          slot_id: r.slot_id,
          asset_id: r.asset_id,
          asset_name: r.asset_name ?? null,
          node_id: r.node_id ?? null,
          input_key: r.input_key ?? null,
          image_no: r.image_no ?? null,
          semantic: r.semantic ?? 'reference',
          enabled: r.enabled !== false,
        }))
      await submitRegenerateWorkspace(target.id, {
        target: 'clip',
        workflow_id: deps.clipWorkflowId(),
        final_prompt: videoPrompt.value,
        references,
        idempotency_key: `clip-rerun-${target.id}-${Date.now()}`,
      })
      message.success('已按当前参数提交重新生成，完成后将替换原视频')
      queryClient.invalidateQueries({ queryKey: ['scenes', projectId.value] })
      queryClient.invalidateQueries({ queryKey: ['jobs', projectId.value, 'generate_clip'] })
      deps.onSubmitted()
      // 提交成功后稍作停留让提示可见，再关闭镜头详情
      window.setTimeout(() => deps.onClose(), 1000)
    } catch (e: unknown) {
      message.error(`重新生成失败: ${(e as Error)?.message ?? e}`)
    } finally {
      directRegenLoading.value = false
    }
  }

  /** 主按钮：未生成过直接提交；已生成/在生成中按当前提示词与参考图重新生成 */
  function handleClipActionClick() {
    if (!shot.value) return
    if (!deps.readinessAll()) return
    if (!clipActionIsRegenerate.value) {
      void generateFromDrawer('clip')
      return
    }
    void regenerateClipDirect()
  }

  /** 生成分镜图（未确认关键帧时主按钮走这里） */
  function handleGenerateKeyframe() {
    if (!shot.value || !deps.readinessAll()) return
    void generateFromDrawer('keyframe')
  }

  return {
    shot,
    videoPrompt,
    negativePrompt,
    negativeSupported,
    defaultNegativePrompt: DEFAULT_NEGATIVE_PROMPT,
    savingPrompts,
    refBindingsLoading,
    refBindingsSaving,
    bindings,
    chainFrameStatus,
    unboundSlots,
    assetOptions,
    generating,
    directRegenLoading,
    clipActionIsRegenerate,
    clipActionLabel,
    open,
    savePrompts,
    saveRefBindings,
    addBinding,
    removeBinding,
    setBindingAsset,
    setBindingEnabled,
    handleClipActionClick,
    handleGenerateKeyframe,
    regenerateClipDirect,
  }
}
