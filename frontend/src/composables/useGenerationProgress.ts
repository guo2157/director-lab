import { computed, onBeforeUnmount, ref, type Ref } from 'vue'

/**
 * 生成进度轮询（资产库 / 分镜图共用）。
 *
 * 设计要点：
 * - 有在途任务时按 interval 轮询，全部结束自动停表，避免空转请求；
 * - 「全部生成完成」只在本次会话真正提交过任务后才提示（notifyOnComplete），
 *   避免用户只是打开页面就收到完成提示；
 * - 任务可能在两次轮询之间就结束，因此提交成功后先立即拉一次，
 *   并在 in-flight 结束时再补一次，保证 done/total 与「已全部完成」是准的。
 */
export interface GenerationProgressOptions<T> {
  fetcher: () => Promise<T>
  /** 轮询间隔（ms），默认 2500 */
  interval?: number
  /** 进度变化时回调，便于页面同步刷新实体数据（如 portrait_asset_id） */
  onChange?: (data: T) => void
  /** 本次会话提交过任务后，在 in-flight 结束且全部完成时触发一次 */
  onAllComplete?: (data: T) => void
}

interface ProgressShape {
  has_inflight: boolean
  all_done: boolean
  inflight_count: number
}

export function useGenerationProgress<T extends ProgressShape>(opts: GenerationProgressOptions<T>) {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const polling = ref(false)
  const error = ref<string | null>(null)
  /** 本次会话是否提交过生成任务：决定是否给出「全部完成」提示 */
  const sessionStarted = ref(false)

  const intervalMs = opts.interval ?? 2500
  let timer: number | null = null
  /** 防重入：轮询期间叠加的触发合并成一次 */
  let inFlightFetch = false
  let pendingRefetch = false
  /** 上一次轮询时是否还有在途任务，用于识别「刚刚全部结束」 */
  let sawInflight = false
  let disposed = false
  /** 连续失败次数：轮询期间偶发超时不应直接停表，重试几次仍失败才提示 */
  let failures = 0
  const MAX_FAILURES = 3

  const hasInflight = computed(() => Boolean(data.value?.has_inflight))
  const inflightCount = computed(() => data.value?.inflight_count ?? 0)
  const allDone = computed(() => Boolean(data.value?.all_done))

  function stop() {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
    polling.value = false
  }

  async function fetchOnce(): Promise<T | null> {
    if (disposed) return null
    if (inFlightFetch) {
      pendingRefetch = true
      return null
    }
    inFlightFetch = true
    loading.value = true
    try {
      const res = await opts.fetcher()
      if (disposed) return null
      const prev = data.value as T | null
      data.value = res
      error.value = null
      failures = 0

      const inflightNow = Boolean(res?.has_inflight)
      // in-flight 结束：停表并（仅在本次会话提交过任务时）提示全部完成
      if (!inflightNow) {
        stop()
        if (sawInflight && sessionStarted.value) {
          if (res?.all_done) opts.onAllComplete?.(res)
          sessionStarted.value = false
        }
      }
      sawInflight = inflightNow

      // 完成/失败数量变化时通知页面同步实体数据（图片 URL、齐全度等）
      if (
        prev &&
        (prev.inflight_count !== res?.inflight_count ||
          (prev as unknown as { done?: number }).done !== (res as unknown as { done?: number }).done)
      ) {
        opts.onChange?.(res)
      }
      return res
    } catch (e) {
      failures += 1
      // 偶发超时/断网：还有在途任务就继续轮询，连续失败多次才停表并提示
      if (polling.value && failures < MAX_FAILURES) {
        return null
      }
      error.value = e instanceof Error ? e.message : '进度获取失败'
      stop()
      return null
    } finally {
      inFlightFetch = false
      loading.value = false
      if (pendingRefetch) {
        pendingRefetch = false
        if (!disposed && polling.value) void fetchOnce()
      }
    }
  }

  /**
   * 提交生成任务成功后调用：立即拉一次建立「有在途」基线，
   * 并在需要时启动轮询、开启「全部完成」提示。
   */
  async function start(opts2?: { notifyOnComplete?: boolean }) {
    sessionStarted.value = opts2?.notifyOnComplete ?? true
    const first = await fetchOnce()
    if (disposed) return
    if (first === null) {
      // 首次拉取失败（刚提交任务时最可能遇到）：先开轮询，后续成功即恢复
      beginPolling()
      return
    }
    if (data.value?.has_inflight) beginPolling()
    else {
      // 任务可能在提交后极短时间内已结束：再拉一次确认，避免显示旧进度
      const again = await fetchOnce()
      if (again === null) beginPolling()
      else if (data.value?.has_inflight) beginPolling()
    }
  }

  /** 只刷新一次，不弹完成提示（页面挂载/手动刷新用） */
  async function refresh() {
    await fetchOnce()
  }

  /** 重新开始失败任务等场景：把当前在途视为新的会话基线 */
  function markSessionStarted() {
    sessionStarted.value = true
  }

  function beginPolling() {
    if (disposed) return
    polling.value = true
    if (timer !== null) return
    timer = window.setInterval(() => void fetchOnce(), intervalMs)
  }

  /** 有在途任务时才启动轮询（页面挂载时调用：用户刷新页面后仍能看到进度） */
  async function bootstrap() {
    const res = await fetchOnce()
    if (res === null) {
      // 首次失败也先轮询：多数是后端刚重启/瞬时超时，恢复后自动继续
      beginPolling()
      return
    }
    if (res.has_inflight) beginPolling()
  }

  onBeforeUnmount(() => {
    disposed = true
    stop()
  })

  return {
    data,
    loading,
    polling,
    error,
    hasInflight,
    inflightCount,
    allDone,
    start,
    refresh,
    bootstrap,
    beginPolling,
    stop,
    markSessionStarted,
    fetchOnce,
  }
}
