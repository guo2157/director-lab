import type { Router } from 'vue-router'

/**
 * 统一导航服务（V2 双栏工作台 Phase 2）
 *
 * 设计约束：
 * 1. 这是前端唯一允许执行路由跳转的业务入口（Agent 导航、页面间跳转统一走这里），
 *    避免路由名/参数散落在各个组件里。
 * 2. 导航 ≠ 执行：本服务只做页面跳转与结果上报，不触发任何生成/任务创建。
 * 3. 校验顺序固定：白名单 → 必需参数 → 未保存保护 → 跳转 → 上报。
 * 4. 失败不谎报：捕获 router 错误返回 failed，由调用方展示真实提示。
 */

/** 导航错误码（与后端 workspace_nav.NavErrorCode 对齐） */
export type NavErrorCode =
  | 'nav_ok'
  | 'nav_invalid_page'
  | 'nav_missing_project'
  | 'nav_invalid_params'
  | 'nav_blocked'
  | 'nav_failed'

export interface NavigateRequest {
  /** 目标页面（必须命中 PAGE_ROUTES 白名单） */
  page: string
  /** 项目 ID（项目内页面必需） */
  projectId?: string
  /** 集数（可选，正整数） */
  episodeNo?: number
  /** 页内 Tab（如 assets 的 character/scene/prop/voice） */
  tab?: string
  /** 实体类型 */
  entityType?: 'character' | 'scene' | 'prop' | 'shot' | 'asset' | 'composition' | 'export'
  /** 实体 ID */
  entityId?: string
  /** 页面内焦点（如 edit/review/result） */
  focus?: string
  /** 导航理由（用于上报与界面提示） */
  reason?: string
}

export interface NavResult {
  ok: boolean
  code: NavErrorCode
  message: string
  /** 实际落地的路由名与完整路径（成功时提供） */
  routeName?: string
  fullPath?: string
}

/**
 * 页面白名单 → 前端路由名映射（与后端 workspace_nav.NAV_PAGE_WHITELIST 保持一致）。
 * 2026-11-20：移除已下线的 `jobs`（任务中心已删，保留会让 Agent 声称「已切到任务页」
 * 而前端只能落到作品列表）；`estimate` 评估页同样已下线，不在此列。
 */
export const PAGE_ROUTES: Record<string, string> = {
  projects: 'projects',
  director: 'director',
  script: 'script',
  parse: 'parse',
  'storyboard-images': 'storyboard-images',
  storyboard: 'storyboard',
  assets: 'assets',
  compose: 'compose',
  settings: 'settings',
  'series-list': 'series-list',
}

/** 需要 projectId 的页面 */
export const PROJECT_REQUIRED_PAGES = new Set([
  'script',
  'parse',
  'storyboard-images',
  'storyboard',
  'assets',
  'compose',
])

/** 页面上下文版本号（乐观并发控制）：上报成功或检测到冲突后同步为服务端最新值 */
let contextRevision = 0
/** 冲突回调签名：由业务层展示真实提示，不静默覆盖、不谎报成功 */
export type ContextConflictInfo = { currentRevision: number; expectedRevision: number }
type ContextConflictHandler = (info: ContextConflictInfo) => void
let contextConflictHandler: ContextConflictHandler | null = null

/** 业务层注册上下文冲突回调（组件卸载时传 null 注销） */
export function setContextConflictHandler(handler: ContextConflictHandler | null) {
  contextConflictHandler = handler
}

/** 当前页面上下文版本号（供调试与上报前判断） */
export function getContextRevision(): number {
  return contextRevision
}

/**
 * 初始化上下文版本号：应用启动时读取服务端当前快照。
 * 不做这一步，页面刷新后本地 revision 从 0 开始，首次上报必然 409。
 * 读取失败保持 0，由冲突回调如实提示，不做假同步。
 */
export async function initContextRevision(): Promise<void> {
  try {
    const base = import.meta.env.BASE_URL || '/'
    const prefix = base.endsWith('/') ? base.slice(0, -1) : base
    const res = await fetch(`${prefix}/api/workspace/page-context`)
    if (!res.ok) return
    const body = (await res.json()) as { revision?: number }
    if (typeof body?.revision === 'number') contextRevision = body.revision
  } catch {
    /* 保持 0，首次上报可能 409 */
  }
}

/** 未保存保护回调：返回 true 表示允许离开 */
export type PendingGuard = () => Promise<boolean> | boolean

let pendingGuard: PendingGuard | null = null

/** 业务页面注册未保存保护（组件卸载时必须传 null 注销） */
export function setPendingGuard(guard: PendingGuard | null) {
  pendingGuard = guard
}

/** 上报导航结果，让后端知道导航是否真的落地（失败不阻断主流程） */
async function reportNavResult(req: NavigateRequest, result: NavResult) {
  const expectedRevision = contextRevision
  try {
    const base = import.meta.env.BASE_URL || '/'
    const prefix = base.endsWith('/') ? base.slice(0, -1) : base
    const res = await fetch(`${prefix}/api/workspace/page-context`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page: req.page,
        project_id: req.projectId ?? null,
        episode_no: req.episodeNo ?? null,
        tab: req.tab ?? null,
        entity_type: req.entityType ?? null,
        entity_id: req.entityId ?? null,
        focus: req.focus ?? null,
        reason: req.reason ?? null,
        nav_result: result.code,
        nav_ok: result.ok,
        route_name: result.routeName ?? null,
        full_path: result.fullPath ?? null,
        expected_revision: expectedRevision,
      }),
    })
    // 409：服务端上下文已被更新。同步本地版本号使后续上报可成功，
    // 同时把冲突如实交回业务层提示；不重发、不覆盖服务端快照。
    if (res.status === 409) {
      let currentRevision = expectedRevision
      try {
        const body = (await res.json()) as { detail?: { current_revision?: number } }
        const cur = body?.detail?.current_revision
        if (typeof cur === 'number') currentRevision = cur
      } catch {
        /* 解析失败保留原值 */
      }
      contextRevision = currentRevision
      contextConflictHandler?.({ currentRevision, expectedRevision })
      return
    }
    if (res.ok) {
      try {
        const body = (await res.json()) as { revision?: number }
        if (typeof body?.revision === 'number') contextRevision = body.revision
      } catch {
        /* 解析失败不影响导航 */
      }
    }
  } catch {
    /* 上报失败不影响用户操作 */
  }
}

/**
 * 执行工作区导航。
 * @param router vue-router 实例（由调用方注入，避免服务层直接依赖应用单例）
 */
export async function navigateWorkspace(router: Router, req: NavigateRequest): Promise<NavResult> {
  // 1. 白名单校验
  const routeName = PAGE_ROUTES[req.page]
  if (!routeName) {
    const result: NavResult = {
      ok: false,
      code: 'nav_invalid_page',
      message: `不支持的页面：${req.page}`,
    }
    void reportNavResult(req, result)
    return result
  }

  // 2. 必需参数校验
  if (PROJECT_REQUIRED_PAGES.has(routeName) && !req.projectId) {
    const result: NavResult = {
      ok: false,
      code: 'nav_missing_project',
      message: '该页面需要先选择一个项目',
    }
    void reportNavResult(req, result)
    return result
  }

  if (req.episodeNo !== undefined && (!Number.isInteger(req.episodeNo) || req.episodeNo < 1)) {
    const result: NavResult = {
      ok: false,
      code: 'nav_invalid_params',
      message: '集数必须为正整数',
    }
    void reportNavResult(req, result)
    return result
  }

  // 3. 未保存保护
  if (pendingGuard) {
    try {
      const allowed = await pendingGuard()
      if (!allowed) {
        const result: NavResult = {
          ok: false,
          code: 'nav_blocked',
          message: '当前页面有未保存内容，导航已取消',
        }
        void reportNavResult(req, result)
        return result
      }
    } catch {
      /* 保护回调异常不阻断导航 */
    }
  }

  // 4. 执行跳转：定位参数统一走 query，不污染现有 params
  const query: Record<string, string> = {}
  if (req.tab) query.tab = req.tab
  if (req.episodeNo !== undefined) query.episode = String(req.episodeNo)
  if (req.entityId) query.entityId = req.entityId
  if (req.focus) query.focus = req.focus
  if (req.projectId && !PROJECT_REQUIRED_PAGES.has(routeName)) query.projectId = req.projectId

  try {
    const params = PROJECT_REQUIRED_PAGES.has(routeName) && req.projectId
      ? { id: req.projectId }
      : undefined
    await router.push({ name: routeName, params, query: Object.keys(query).length ? query : undefined })

    const result: NavResult = {
      ok: true,
      code: 'nav_ok',
      message: `已切换到「${req.page}」页面`,
      routeName,
      fullPath: router.currentRoute.value.fullPath,
    }
    void reportNavResult(req, result)
    return result
  } catch (e) {
    const result: NavResult = {
      ok: false,
      code: 'nav_failed',
      message: `页面跳转失败：${e instanceof Error ? e.message : String(e)}`,
    }
    void reportNavResult(req, result)
    return result
  }
}

/**
 * 便捷方法：从后端 assistant payload 中的 navigate_workspace 指令执行导航。
 * payload 形如 { route_action: 'navigate_workspace', route_name: 'storyboard', route_label: '分镜' }
 */
export async function navigateFromPayload(
  router: Router,
  payload: Record<string, unknown> | null,
  fallbackProjectId?: string,
): Promise<NavResult | null> {
  if (!payload || payload.route_action !== 'navigate_workspace') return null
  const routeName = typeof payload.route_name === 'string' ? payload.route_name : null
  if (!routeName) {
    return {
      ok: false,
      code: 'nav_invalid_page',
      message: '未识别的导航目标，请说明想去的页面',
    }
  }
  // payload 下发的 project_id 优先：新建项目后需要跳进「刚创建的项目」，
  // 此时当前页面可能还没有项目 ID（fallbackProjectId 为空）。
  const payloadProjectId =
    typeof payload.project_id === 'string' && payload.project_id ? payload.project_id : undefined
  return navigateWorkspace(router, {
    page: routeName,
    projectId: payloadProjectId ?? fallbackProjectId,
    reason: typeof payload.route_label === 'string' ? `Agent 导航：${payload.route_label}` : 'Agent 导航',
  })
}
