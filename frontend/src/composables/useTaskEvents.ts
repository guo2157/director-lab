import { onUnmounted, reactive, ref } from 'vue'
import { apiUrl } from '@/api/client'
import { backfillTaskEvents } from '@/api/tasks'
import type { TaskCardData, TaskEvent } from '@/api/tasks'

/**
 * 统一任务事件流消费（进程级单例）：
 * - 订阅 /api/tasks/events SSE（seq 严格大于判定，与后端语义一致）
 * - 断线重连时通过 /api/tasks/events/backfill 按 lastSeq 补齐丢失事件
 * - 维护 task_id → 卡片快照（status/progress/事件合并），供任务卡片单卡片动态更新
 *
 * 设计约束（Phase 1.5 地基修正）：
 * 1. 连接是全局单例，组件卸载只做引用计数回收，不直接关闭连接，
 *    否则任意页面卸载都会让其他页面的任务卡片停止更新（间歇性、极难复现）。
 * 2. 引用计数归零才真正断开；只有显式 dispose() 才置 disposed，禁止用 stopped 永久阻断重连。
 * 3. EventSource 地址必须经 apiUrl() 生成，兼容子路径部署。
 */

/** task_id → 卡片快照（SSE 事件合并后的最新状态） */
const taskSnapshots = reactive<Record<string, Partial<TaskCardData> & { lastEvent?: TaskEvent }>>({})
const connected = ref(false)

let eventSource: EventSource | null = null
let lastSeq = 0
let backfilling = false
/** 订阅方引用计数 */
let refCount = 0
/** 仅显式销毁时置位，release 不置位 */
let disposed = false

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
    if (v == null) continue
    // task.step 事件的 message 是后端真实阶段文案（当前步骤展示）
    if (k === 'message') {
      snap.current_message = String(v)
      continue
    }
    ;(snap as Record<string, unknown>)[k] = v
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
  if (eventSource || disposed) return
  try {
    eventSource = new EventSource(apiUrl(`/tasks/events?last_seq=${lastSeq}`))
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
  eventSource?.close()
  eventSource = null
  connected.value = false
}

/** 订阅方卸载：只回收引用计数，计数归零才断开连接 */
function release() {
  refCount = Math.max(0, refCount - 1)
  if (refCount === 0) disconnect()
}

/** 显式销毁（应用退出/登出等场景），之后不再自动重连 */
function dispose() {
  disposed = true
  refCount = 0
  disconnect()
}

/**
 * 组件侧使用入口：内部共享同一 SSE 连接与快照表。
 * 必须在组件 setup 中调用，卸载时自动 release。
 */
export function useTaskEvents() {
  refCount += 1
  onUnmounted(release)
  return {
    taskSnapshots,
    connected,
    registerCard,
    connect,
    disconnect,
    dispose,
    backfill,
  }
}
