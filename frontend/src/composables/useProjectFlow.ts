import { computed, type ComputedRef } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { getCurrentScript, getParseStatus, getProject, getScriptAgentStatus } from '@/api/projects'
import { stageIndex } from '@/constants/stages'

/**
 * 项目制作流程状态（左侧「制作流程」栏与页内共用的唯一推导口径）。
 *
 * 为什么需要它：解析从「剧本阶段的子步骤」独立成节点后，6 个步骤的完成态
 * 不再能只看 project.current_stage —— 后端 derive_progress 在「已保存剧本、
 * 尚无镜头、estimated_text_hash 为空」时恒返回 estimate，前端实际路径
 * （保存剧本 → script-agent 解析）根本到不了 select_range / parse。
 * 因此剧本与解析两步改由**事实**判定（有没有保存版本、有没有解析记录），
 * 资产库及之后仍沿用 current_stage 序号。
 *
 * 数据全部走 TanStack Query，queryKey 与各页面完全一致（['project', id] /
 * ['currentScript', id] / ['parse-status', id] / ['script-agent-status', id]），
 * 因此左栏与页内共享同一份缓存，不会重复请求。
 */

export type StepKey = 'script' | 'parse' | 'assets' | 'storyboard-images' | 'storyboard' | 'compose'
export type StepStatus = 'locked' | 'todo' | 'running' | 'done' | 'failed'

export interface FlowStep {
  key: StepKey
  label: string
  /** 该步骤覆盖的后端 current_stage 枚举，用于「正在做」判定 */
  stages: readonly string[]
  status: StepStatus
  /** locked 时的原因（按钮 title / aria 文案），其余状态为空串 */
  hint: string
}

interface StepDef {
  key: StepKey
  label: string
  stages: readonly string[]
  /** 前置步骤：未 done 时本步骤锁定 */
  requires: StepKey | null
  lockedHint: string
}

/**
 * 步骤定义。stages 只用于「当前正在做哪一步」，
 * script / parse 两步的完成态另有事实来源（见 steps computed）。
 */
const STEP_DEFS: readonly StepDef[] = [
  { key: 'script', label: '剧本', stages: ['estimate'], requires: null, lockedHint: '' },
  {
    key: 'parse',
    label: '解析',
    stages: ['select_range', 'parse'],
    requires: 'script',
    lockedHint: '请先在剧本页保存剧本版本',
  },
  {
    key: 'assets',
    label: '资产库',
    stages: ['characters', 'scenes'],
    requires: 'parse',
    lockedHint: '请先完成剧本解析',
  },
  {
    key: 'storyboard-images',
    label: '分镜图',
    stages: ['assets_review', 'keyframes'],
    requires: 'assets',
    lockedHint: '请先完成资产库审核',
  },
  {
    key: 'storyboard',
    label: '分镜看板',
    stages: ['clips', 'audio'],
    requires: 'storyboard-images',
    lockedHint: '请先完成分镜图',
  },
  {
    key: 'compose',
    label: '合成与导出',
    stages: ['compose', 'export', 'done'],
    requires: 'storyboard',
    lockedHint: '请先完成分镜视频',
  },
]

/** 左栏渲染顺序（供组件 v-for 使用，避免组件里再写一遍 key 列表） */
export const FLOW_STEP_KEYS: readonly StepKey[] = STEP_DEFS.map((d) => d.key)

/**
 * 按集号聚合解析记录：同一集可能按不同 asset_type 存多条，
 * 任一条非 succeeded 即视为该集未完成（与解析页集数网格口径一致）。
 */
function episodeStatusMap(
  records: ReadonlyArray<{ episode_no: number; status: string }>,
): Map<number, string> {
  const map = new Map<number, string>()
  for (const r of records) {
    const prev = map.get(r.episode_no)
    if (prev === undefined) map.set(r.episode_no, r.status)
    else if (prev === 'succeeded' && r.status !== 'succeeded') map.set(r.episode_no, r.status)
  }
  return map
}

export function useProjectFlow(projectId: ComputedRef<string>, routeName: ComputedRef<string>) {
  const enabled = computed(() => !!projectId.value)

  const { data: project } = useQuery({
    queryKey: computed(() => ['project', projectId.value]),
    queryFn: () => getProject(projectId.value),
    enabled,
  })

  /** 已保存的剧本版本：剧本步骤的完成态唯一事实来源 */
  const { data: script } = useQuery({
    queryKey: computed(() => ['currentScript', projectId.value]),
    queryFn: () => getCurrentScript(projectId.value),
    enabled,
  })

  /** 集级解析记录：解析步骤的完成 / 失败事实来源 */
  const { data: parseStatus } = useQuery({
    queryKey: computed(() => ['parse-status', projectId.value]),
    queryFn: () => getParseStatus(projectId.value),
    enabled,
  })

  /**
   * 最近一个 script_agent 任务：解析「进行中」的事实来源。
   * 只在任务真的在跑时轮询（5s），无任务 / 已结束时停表 —— 左栏在非解析页
   * 也会读这个 key，恒定轮询会让每个页面都空转请求。
   */
  const { data: agentStatus } = useQuery({
    queryKey: computed(() => ['script-agent-status', projectId.value]),
    queryFn: () => getScriptAgentStatus(projectId.value),
    enabled,
    refetchInterval: (query) => {
      const status = (query.state.data as { job?: { status?: string } } | undefined)?.job?.status
      return status === 'queued' || status === 'running' ? 5000 : false
    },
  })

  const currentStage = computed(() => project.value?.current_stage ?? '')
  const currentStageIndex = computed(() => stageIndex(currentStage.value))

  /** 剧本步骤完成：存在已保存版本（解析一律以已保存版本为准） */
  const scriptSaved = computed(() => Boolean(script.value?.exists && script.value.script_version_id))

  const episodes = computed(() => episodeStatusMap(parseStatus.value?.records ?? []))
  const totalEpisodes = computed(() => parseStatus.value?.total_episodes ?? episodes.value.size)
  const succeededEpisodes = computed(
    () => [...episodes.value.values()].filter((s) => s === 'succeeded').length,
  )
  const failedEpisodes = computed(
    () => [...episodes.value.values()].filter((s) => s === 'failed').length,
  )

  const agentJobStatus = computed(() => agentStatus.value?.job?.status ?? '')
  const parseRunning = computed(
    () =>
      agentJobStatus.value === 'queued' ||
      agentJobStatus.value === 'running' ||
      [...episodes.value.values()].includes('running'),
  )
  /** 有失败集且没有任务在跑：失败态常驻可见（刷新后 parseJobId 丢失也不影响） */
  const parseFailed = computed(() => failedEpisodes.value > 0 && !parseRunning.value)

  /**
   * 解析完成：三条任一成立即视为完成。
   * 1. current_stage 已越过 parse（characters 及之后）—— 后端已确认产出；
   * 2. 已有镜头（shot_total > 0）—— 解析真的落库了；
   * 3. 集级记录全成功且覆盖总集数。
   */
  const parseDone = computed(() => {
    if (currentStageIndex.value > stageIndex('parse')) return true
    if ((project.value?.shot_total ?? 0) > 0) return true
    const total = totalEpisodes.value
    if (total <= 0) return false
    // 集级记录全成功 ≠ 已落库：实体写入是随后的独立步骤，其间左侧栏会过早点亮
    // 「已完成」并解锁分镜图（用户点进去空白）。因此必须同时确认库里已有镜头。
    return succeededEpisodes.value >= total && episodes.value.size >= total && (project.value?.shot_total ?? 0) > 0
  })

  const steps = computed<Record<StepKey, FlowStep>>(() => {
    const out = {} as Record<StepKey, FlowStep>
    for (const def of STEP_DEFS) {
      // 前置步骤未完成 → 锁定。按定义顺序遍历，前置项必然已计算。
      // 解析特殊处理：继续解析 / 重新解析时它的展示态是 running，但库里
      // 已有上一次的产出，不能让资产库及之后重新变锁（否则解析期间无法离开本页）。
      const locked = def.requires
        ? def.requires === 'parse'
          ? !parseDone.value
          : out[def.requires].status !== 'done'
        : false
      let status: StepStatus
      if (locked) {
        status = 'locked'
      } else if (def.key === 'script') {
        // 保存是同步操作，不存在「进行中」
        status = scriptSaved.value ? 'done' : 'todo'
      } else if (def.key === 'parse') {
        // 运行中优先于已完成：继续解析剩余集 / 重新解析时，库里还留着上一次
        // 成功的镜头（shot_total > 0 会让 parseDone 恒为真）。若先判 done，
        // 左栏显示「已完成」勾，而页头同时显示「解析中」，两处口径打架。
        if (parseRunning.value) status = 'running'
        else if (parseDone.value) status = 'done'
        else if (parseFailed.value) status = 'failed'
        else status = 'todo'
      } else {
        const inStep = (def.stages as readonly string[]).includes(currentStage.value)
        const maxIdx = Math.max(...def.stages.map((s) => stageIndex(s)))
        // 解析任务在跑时，current_stage 可能已被首批产出推进到 characters，
        // 但资产库/分镜图此刻并没有人在做 —— 只有解析在进行。
        // 不压掉会出现左栏「解析 进行中」+「资产库 进行中」两个绿点并存。
        if (currentStage.value === 'done' || currentStageIndex.value > maxIdx) status = 'done'
        else if (inStep && !parseRunning.value) status = 'running'
        else status = 'todo'
      }
      out[def.key] = {
        key: def.key,
        label: def.label,
        stages: def.stages,
        status,
        hint: locked ? def.lockedHint : '',
      }
    }
    return out
  })

  /** 当前页对应的步骤（6 步均为独立路由，不再有「回落到父步骤」） */
  const activeKey = computed<StepKey | null>(() => {
    const key = routeName.value as StepKey
    return STEP_DEFS.some((d) => d.key === key) ? key : null
  })

  return {
    steps,
    activeKey,
    project,
    script,
    parseStatus,
    agentStatus,
    scriptSaved,
    parseDone,
    parseRunning,
    parseFailed,
    totalEpisodes,
    succeededEpisodes,
    failedEpisodes,
  }
}
