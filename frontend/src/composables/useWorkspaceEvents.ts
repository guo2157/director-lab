import { reactive, readonly, ref } from 'vue'
import { apiUrl } from '@/api/client'
import { backfillWorkspaceEvents } from '@/api/workspace'
import type { WorkspaceEvent } from '@/api/workspace'

/**
 * 工作台事件流消费（进程级单例，V2 Phase 3）
 *
 * 与 useTaskEvents 的关键差异（吸取其单例冲突教训）：
 * 1. 不注册 onUnmounted，不维护组件引用计数。工作台事件是应用级事实流，
 *    任何组件卸载都不应导致连接断开；只有显式 dispose() 才断开。
 * 2. EventSource 地址必须经 apiUrl() 生成，兼容子路径部署（不硬编码 /api）。
 * 3. 断线重连由 EventSource 自身负责；重连成功（onopen）后按 lastSeq 补齐。
 * 4. 只做「事件收集与分发」，不改写业务状态；具体消费者自行订阅。
 */

/** 最近一次收到的事件序列号（断线补齐基准） */
const lastSeq = ref(0)
/** SSE 连接状态（供 UI 展示，不参与业务判断） */
const connected = ref(false)
/** 按 event_type 归集的最新事件，供 UI 快速读取（不保留全量历史，避免内存增长） */
const latestByType = reactive<Record<string, WorkspaceEvent>>({})

let eventSource: EventSource | null = null
let started = false
let backfilling = false

/** 事件监听者：自定义回调（如导航提示、审批刷新） */
type Listener = (ev: WorkspaceEvent) => void
const listeners = new Set<Listener>()

/** 订阅事件；返回取消订阅函数 */
export function onWorkspaceEvent(fn: Listener): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function dispatch(ev: WorkspaceEvent): void {
  if (typeof ev.seq === 'number' && ev.seq <= lastSeq.value) return
  if (typeof ev.seq === 'number') lastSeq.value = ev.seq
  if (ev.event_type) latestByType[ev.event_type] = ev
  for (const fn of listeners) {
    try {
      fn(ev)
    } catch {
      // 单个监听者异常不影响其他监听者与连接
    }
  }
}

/** 断线补齐：按 lastSeq 拉取丢失事件 */
async function backfill(): Promise<void> {
  if (backfilling) return
  backfilling = true
  try {
    const res = await backfillWorkspaceEvents(lastSeq.value)
    for (const ev of res.items ?? []) dispatch(ev)
  } catch {
    // 补齐失败由 SSE 重连兜底，不在此处重试
  } finally {
    backfilling = false
  }
}

/** 启动事件流（幂等：重复调用不会重复建连接） */
export function startWorkspaceEvents(): void {
  if (started || eventSource) return
  started = true
  try {
    eventSource = new EventSource(apiUrl(`/workspace/events?last_seq=${lastSeq.value}`))
    eventSource.onopen = () => {
      connected.value = true
      void backfill()
    }
    eventSource.onmessage = (e) => {
      connected.value = true
      try {
        dispatch(JSON.parse(e.data) as WorkspaceEvent)
      } catch {
        // 非 JSON 消息忽略（心跳为注释行，不会触发 onmessage）
      }
    }
    eventSource.onerror = () => {
      // EventSource 会自动重连；重连成功后 onopen 触发 backfill
      connected.value = false
    }
  } catch {
    connected.value = false
    started = false
  }
}

/** 显式销毁（应用退出/登出）：之后不再自动重连 */
export function disposeWorkspaceEvents(): void {
  started = false
  eventSource?.close()
  eventSource = null
  connected.value = false
  listeners.clear()
}

/**
 * 组件侧只读入口：拿连接状态与最新事件，不触发连接建立。
 * 连接建立统一由 startWorkspaceEvents() 在应用层调用一次。
 */
export function useWorkspaceEvents() {
  return {
    lastSeq: readonly(lastSeq),
    connected: readonly(connected),
    latestByType: readonly(latestByType),
    onEvent: onWorkspaceEvent,
    backfill,
  }
}
