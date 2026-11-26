"""Agent Runtime 恢复驱动（Phase B B5）。

设计（见 plan/phase-b-agent-prompts.md 〇 设计说明）：
- 恢复请求 = agent_plan_step.resume_requested_at 标志位，由 bridge_job_event /
  approvals.resolve_confirmation 与业务写入同一事务打上，不在 Job 执行线程或
  API 请求里同步调用 Runtime（避免重入、长事务、死锁）。
- Worker 中的 agent-resume 守护线程每 2 秒认领带标记的步骤并 tick；
  每 30 秒兜底扫描一次（Job 已终态 / 审批已裁决 / 超时 running / tick 中途崩溃）。
- 标记丢失、进程重启都由兜底扫描补齐，因此不需要事件游标表。
- workspace_event 的 agent.* 事件只用于前端展示，不作为恢复驱动。

认领语义：
- 条件 UPDATE 把 resume_reason 改写为 "claimed:<原 reason>"（列长 32，原 reason 截断到 24），
  只有 rowcount==1 的执行者获得该 Goal 的推进权；tick 处理时剥掉 claimed: 前缀。
- tick 返回 busy（其他执行者正在推进）时把 reason 还原，下一轮重新认领。
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

_CLAIM_PREFIX = "claimed:"
_SWEEP_INTERVAL_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 2.0
_STALE_TICK_MINUTES = 5


def _base_reason(reason: str | None) -> str:
    """去掉认领前缀，返回业务原因。"""
    reason = reason or "manual"
    if reason.startswith(_CLAIM_PREFIX):
        return reason[len(_CLAIM_PREFIX):] or "manual"
    return reason


def claim_marked_goals(db: Session, *, limit: int = 5) -> list[tuple[str, str]]:
    """认领一批带恢复标记的 Goal，返回 [(goal_id, 认领 token)]。

    同一 Goal 的多个标记步骤一次性认领（按 Goal 去重，一次 tick 处理全部标记）。
    """
    from app.models.agent import AgentGoal, AgentPlan, AgentPlanStep

    goal_ids = list(
        db.scalars(
            select(AgentPlan.goal_id)
            .join(AgentPlanStep, AgentPlanStep.plan_id == AgentPlan.id)
            .where(
                AgentPlanStep.resume_requested_at.isnot(None),
                AgentPlanStep.resume_reason.is_(None)
                | ~AgentPlanStep.resume_reason.startswith(_CLAIM_PREFIX),
            )
            .distinct()
            .limit(limit)
        ).all()
    )
    if not goal_ids:
        return []

    active_ids = set(
        db.scalars(
            select(AgentGoal.id).where(
                AgentGoal.id.in_(goal_ids), AgentGoal.status == "active"
            )
        ).all()
    )

    claimed: list[tuple[str, str]] = []
    for goal_id in goal_ids:
        if goal_id not in active_ids:
            continue
        token = uuid.uuid4().hex[:8]
        step_rows = db.scalars(
            select(AgentPlanStep)
            .join(AgentPlan, AgentPlanStep.plan_id == AgentPlan.id)
            .where(
                AgentPlan.goal_id == goal_id,
                AgentPlanStep.resume_requested_at.isnot(None),
            )
        ).all()
        won = 0
        for step in step_rows:
            base = _base_reason(step.resume_reason)
            new_reason = f"{_CLAIM_PREFIX}{base}"[:32]
            result = db.execute(
                update(AgentPlanStep)
                .where(
                    AgentPlanStep.id == step.id,
                    AgentPlanStep.resume_requested_at.isnot(None),
                    AgentPlanStep.resume_reason.is_(None)
                    | ~AgentPlanStep.resume_reason.startswith(_CLAIM_PREFIX),
                )
                .values(resume_reason=new_reason)
            )
            won += int(result.rowcount or 0)
        if won:
            claimed.append((goal_id, token))
    db.commit()
    return claimed


def release_claim(db: Session, goal_id: str, token: str) -> None:
    """tick 忙时归还认领：把 claimed:<reason> 还原为 <reason>，下一轮重新认领。"""
    from app.models.agent import AgentPlan, AgentPlanStep

    step_rows = db.scalars(
        select(AgentPlanStep)
        .join(AgentPlan, AgentPlanStep.plan_id == AgentPlan.id)
        .where(
            AgentPlan.goal_id == goal_id,
            AgentPlanStep.resume_requested_at.isnot(None),
            AgentPlanStep.resume_reason.startswith(_CLAIM_PREFIX),
        )
    ).all()
    for step in step_rows:
        step.resume_reason = _base_reason(step.resume_reason)
    db.commit()


def process_marked(session_factory: Callable[[], Session] | None = None, *, limit: int = 5) -> int:
    """认领并推进一批带恢复标记的 Goal，返回处理的 Goal 数。

    单个 Goal 的异常不影响其他 Goal（捕获并记录日志）。
    """
    from app.services.agent_runtime import tick

    if session_factory is None:
        from app.core.database import SessionLocal as session_factory

    db = session_factory()
    try:
        claimed = claim_marked_goals(db, limit=limit)
    except Exception:  # noqa: BLE001 - 恢复线程不能因认领失败退出
        _log.warning("agent_resume.claim 失败: %s", __import__("traceback").format_exc())
        db.rollback()
        db.close()
        return 0

    processed = 0
    for goal_id, token in claimed:
        try:
            result = tick(session_factory, goal_id, trigger="worker_resume")
            if result.busy:
                release_claim(db, goal_id, token)
                continue
            processed += 1
        except Exception:  # noqa: BLE001
            _log.warning(
                "agent_resume.tick 失败 (goal_id=%s): %s",
                goal_id, __import__("traceback").format_exc(),
            )
            try:
                release_claim(db, goal_id, token)
            except Exception:  # noqa: BLE001
                db.rollback()
    db.close()
    return processed


def sweep(session_factory: Callable[[], Session] | None = None) -> dict:
    """兜底扫描：补打丢失的恢复标记，并回收超时 / 崩溃的推进。

    1. waiting_job 且 Job 已终态 → 标记 job_terminal
    2. waiting_approval 且审批已不是 pending → 标记 approval_resolved
    3. reclaim_stale_steps 回收超时 running 步骤
    4. Goal active + lifecycle=running + last_tick_at 超过 5 分钟且有 pending 步骤 → 直接 tick

    返回 {"marked_job": n, "marked_approval": n, "reclaimed": [...], "failed": [...], "ticked": [...]}
    """
    from app.models import Job
    from app.models.agent import AgentConfirmation, AgentGoal, AgentPlan, AgentPlanStep
    from app.services.agent_runtime import tick
    from app.services.execution_manager import reclaim_stale_steps

    if session_factory is None:
        from app.core.database import SessionLocal as session_factory

    out = {
        "marked_job": 0,
        "marked_approval": 0,
        "reclaimed": [],
        "failed": [],
        "ticked": [],
    }
    db = session_factory()
    try:
        waiting_job = db.scalars(
            select(AgentPlanStep).where(
                AgentPlanStep.status == "waiting_job",
                AgentPlanStep.job_id.isnot(None),
                AgentPlanStep.resume_requested_at.is_(None),
            )
        ).all()
        for step in waiting_job:
            job = db.get(Job, step.job_id)
            if job is None or job.status not in ("succeeded", "failed", "cancelled"):
                continue
            step.resume_requested_at = datetime.now()
            step.resume_reason = "sweep"
            out["marked_job"] += 1

        waiting_approval = db.scalars(
            select(AgentPlanStep).where(
                AgentPlanStep.status == "waiting_approval",
                AgentPlanStep.confirmation_id.isnot(None),
                AgentPlanStep.resume_requested_at.is_(None),
            )
        ).all()
        for step in waiting_approval:
            confirmation = db.get(AgentConfirmation, step.confirmation_id)
            if confirmation is None or confirmation.status == "pending":
                continue
            step.resume_requested_at = datetime.now()
            step.resume_reason = "sweep"
            out["marked_approval"] += 1

        reclaimed = reclaim_stale_steps(db)
        out["reclaimed"] = reclaimed["reclaimed"]
        out["failed"] = reclaimed["failed"]

        stale_before = datetime.now() - timedelta(minutes=_STALE_TICK_MINUTES)
        crashed = db.scalars(
            select(AgentGoal).where(
                AgentGoal.status == "active",
                AgentGoal.lifecycle_state == "running",
                AgentGoal.last_tick_at.isnot(None),
                AgentGoal.last_tick_at < stale_before,
            )
        ).all()
        for goal in crashed:
            has_pending = db.scalar(
                select(AgentPlanStep.id)
                .join(AgentPlan, AgentPlanStep.plan_id == AgentPlan.id)
                .where(AgentPlan.goal_id == goal.id, AgentPlanStep.status == "pending")
                .limit(1)
            )
            if has_pending is None:
                continue
            goal_id = goal.id
            db.commit()
            result = tick(session_factory, goal_id, trigger="worker_sweep")
            if not result.busy:
                out["ticked"].append(goal_id)

        db.commit()
    except Exception:  # noqa: BLE001 - 兜底扫描失败不能拖垮 Worker
        _log.warning("agent_resume.sweep 失败: %s", __import__("traceback").format_exc())
        db.rollback()
    finally:
        db.close()
    return out


def run_resume_loop(
    stop_event: threading.Event,
    session_factory: Callable[[], Session] | None = None,
    *,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
    sweep_interval: float = _SWEEP_INTERVAL_SECONDS,
) -> None:
    """agent-resume 守护线程主体（独立函数便于测试）。

    每 poll_interval 处理一次标记，每 sweep_interval 兜底扫描一次；
    收到 stop_event 后退出。任何异常都不允许让线程死掉。
    """
    import time as _time

    last_sweep = 0.0
    while not stop_event.is_set():
        try:
            process_marked(session_factory)
        except Exception:  # noqa: BLE001
            _log.warning("agent_resume.process_marked 异常: %s", __import__("traceback").format_exc())
        now = _time.monotonic()
        if now - last_sweep >= sweep_interval:
            last_sweep = now
            try:
                sweep(session_factory)
            except Exception:  # noqa: BLE001
                _log.warning("agent_resume.sweep 异常: %s", __import__("traceback").format_exc())
        stop_event.wait(poll_interval)


def start_resume_thread(
    session_factory: Callable[[], Session] | None = None,
) -> tuple[threading.Thread, threading.Event] | None:
    """启动 agent-resume 守护线程；配置关闭时返回 None。"""
    from app.core.config import get_settings

    if not bool(getattr(get_settings(), "agent_resume_enabled", True)):
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_resume_loop,
        args=(stop_event, session_factory),
        name="agent-resume",
        daemon=True,
    )
    thread.start()
    return thread, stop_event
