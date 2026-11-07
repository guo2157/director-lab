import { onUnmounted, reactive, ref } from 'vue'
import { backfillTaskEvents } from '@/api/tasks'
import type { TaskCardData, TaskEvent } from '@/api/tasks'

/**
 * 统一任务事件流消费：
 * - 订阅 /api/tasks/events SSE（seq 严格大于判定，与后端语义一致）
 * - 断线重连时通过 /api/tasks/events/backfill 按 lastSeq 补齐丢失事件
 * - 维护 task_id → 卡片快照（status/progress/事件合并），供任务卡片单卡片动态更新
 */
export function useTaskEvents() {
  /** task_id → 卡片快照（SSE 事件合并后的最新状态） */
  const taskSnapshots = reactive<Record<string, Partial<TaskCardData> & { lastEvent?: TaskEvent }>>({})
  const connected = ref(false)
  let eventSource: EventSource | null = null
  let lastSeq = 0
  let stopped = false
  let backfilling = false

  /** 用初始卡片（assistant payload.task_card）注册快照 */
  function registerCard(card: TaskCardData) {
    const prev = taskSnapshots[card.task_id]
    // 已有更新的事件数据时不回退（初始卡片仅作兜底）
    if (prev?.status && prev.status !== 'pending') return
    taskSnapshots[card.task_id] = { ...card }
  }

  /** 合并一条事件到快照（后发事件覆盖旧状态） */
  function applyEvent(ev: TaskEvent) {
    if (typeof ev.seq === 'number' && ev.seq <= lastSeq) return
    if (typeof ev.seq === 'number') lastSeq = ev.seq
    const snap = (taskSnapshots[ev.task_id] ??= {})
    if (ev.status != null) snap.status = ev.status
    if (ev.progress != null) snap.progress = ev.progress
    // 事件 payload 中的扩展字段（error_code/error_msg/stage 等）合并进快照
    for (const [k, v] of Object.entries(ev)) {
      if (['seq', 'event_type', 'task_id', 'status', 'progress', 'updated_at'].includes(k)) continue
      if (v != null) (snap as Record<string, unknown>)[k] = v
    }
    snap.lastEvent = ev
  }

  /** 断线补齐：按 lastSeq 拉取丢失事件 */
  async function backfill() {
    if (backfilling) return
    backfilling = true
    try {
      const res = await backfillTaskEvents(lastSeq)
      for (const ev of res.items ?? []) applyEvent(ev)
    } catch {
      /* 补齐失败由 SSE 重连兜底 */
    } finally {
      backfilling = false
    }
  }

  function connect() {
    if (eventSource || stopped) return
    try {
      eventSource = new EventSource(`/api/tasks/events?last_seq=${lastSeq}`)
      eventSource.onopen = () => {
        connected.value = true
        void backfill()
      }
      eventSource.onmessage = (ev) => {
        connected.value = true
        try {
          applyEvent(JSON.parse(ev.data) as TaskEvent)
        } catch {
          /* 非 JSON 消息（ping 注释行不会触发 onmessage）忽略 */
        }
      }
      eventSource.onerror = () => {
        // EventSource 自动重连；标记断线，重连成功后由 onopen 触发 backfill
        connected.value = false
      }
    } catch {
      connected.value = false
    }
  }

  function disconnect() {
    stopped = true
    eventSource?.close()
    eventSource = null
    connected.value = false
  }

  onUnmounted(disconnect)

  return { taskSnapshots, connected, registerCard, connect, disconnect, backfill }
}
