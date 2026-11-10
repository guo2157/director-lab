"""Task Orchestrator：统一任务编排层（对话式任务执行改造 Phase 2）。

设计约束（Phase 1 审计结论 + 十阶段方案）：
- Task 是编排层实体，Job 保持底层执行单元不变，不推倒重写执行层
- 所有状态由确定性代码写入；LLM 不得编造状态，事件流是唯一事实来源
- task_event 复用 job_event outbox 语义：全局递增 seq，断线重连以 seq 严格大于判定
- 进度事件不刷屏聊天记录：前端任务卡片按 task_id 动态更新，事件只进 outbox
- 幂等：同一 agent_task_id 只建一个 Task（重复创建返回已有 Task）
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.ids import task_id, task_result_id, task_stage_id
from app.models.production import Job
from app.models.task import Task, TaskEvent, TaskResult, TaskStage

# Task 状态机（终态不可再变更）
_TASK_TRANSITIONS = {
    # queued：已受理待执行（异步任务提交后的初始态）；pending 为历史取值，等价处理
    "queued": {"running", "completed", "failed", "cancelled"},
    "pending": {"running", "completed", "failed", "cancelled"},
    "running": {"completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}

# TaskStage 状态机
_STAGE_TRANSITIONS = {
    "pending": {"running", "succeeded", "failed", "skipped", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "failed": {"running"},  # failed 可重试回 running
    "skipped": set(),
    "cancelled": set(),
}

# DAG 调度器：最大并发阶段数（Phase 2）
MAX_CONCURRENT_STAGES = 8


# ── DAG 调度器（Phase 2） ────────────────────────────────────────


def schedule_ready_stages(db: Session, task: Task) -> int:
    """扫描任务内所有 pending 阶段，自动启动依赖已满足的阶段。

    - 依赖门禁：前置阶段（depends_on）均 succeeded 后启动
    - 并发控制：同时 running 数 ≤ MAX_CONCURRENT_STAGES；
      超限时优先无依赖阶段先发
    - 返回本次启动的阶段数
    """
    if task.status not in ("pending", "running"):
        return 0
    all_stages = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
    if not all_stages:
        return 0
    by_key = {s.stage_key: s for s in all_stages}
    running_count = sum(1 for s in all_stages if s.status == "running")
    remaining = max(0, MAX_CONCURRENT_STAGES - running_count)
    if remaining <= 0:
        return 0
    # 候选队列：pending + 依赖已满足，优先无依赖阶段
    candidates: list[TaskStage] = []
    for s in all_stages:
        if s.status != "pending":
            continue
        deps_ok = True
        for dep_key in (s.depends_on or []):
            dep = by_key.get(dep_key)
            if dep is None or dep.status != "succeeded":
                deps_ok = False
                break
        if deps_ok:
            candidates.append(s)
    # 无依赖的优先（排序稳定：低 index 先发）
    candidates.sort(key=lambda s: (0 if not s.depends_on else 1, s.stage_index))
    started = 0
    for s in candidates:
        if started >= remaining:
            break
        if start_stage(db, task, s):
            started += 1
    return started


def _skip_dependent_stages(db: Session, task: Task, failed_stage: TaskStage) -> None:
    """阶段失败时级联跳过依赖它的阶段：标记为 skipped 并记录阻塞原因。"""
    all_stages = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
    for s in all_stages:
        if s.status not in ("pending", "running"):
            continue
        if not s.depends_on:
            continue
        if failed_stage.stage_key not in s.depends_on:
            continue
        s.status = "skipped"
        s.result = {
            "blocked_by": failed_stage.stage_key,
            "reason": f"前置阶段 {failed_stage.stage_name} 执行失败",
        }
        write_event(
            db,
            task,
            "task.stage_skipped",
            {
                "stage_id": s.id,
                "stage_key": s.stage_key,
                "blocked_by": failed_stage.stage_key,
            },
        )


def write_event(db: Session, task: Task, event_type: str, payload: dict | None = None) -> None:
    """写任务事件（outbox）：任务状态的唯一事实来源。"""
    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=event_type,
            payload=payload,
            created_at=datetime.now(),
        )
    )
    db.flush()


def create_task(
    db: Session,
    *,
    session_id: str | None,
    task_type: str,
    title: str,
    agent_task_id: str | None = None,
    project_id: str | None = None,
    message_id: str | None = None,
) -> Task:
    """创建统一任务（幂等：同一 agent_task_id 已有 Task 时直接返回）。

    创建即写 task.created 事件；任务进入 running 由 start_task 显式触发。
    """
    if agent_task_id:
        existing = (
            db.query(Task)
            .filter(Task.agent_task_id == agent_task_id)
            .order_by(Task.created_at.desc())
            .first()
        )
        if existing is not None:
            return existing
    task = Task(
        id=task_id(),
        session_id=session_id,
        agent_task_id=agent_task_id,
        project_id=project_id,
        task_type=task_type,
        title=title[:200],
        status="queued",
        progress=0,
        message_id=message_id,
        current_step="queued",
        current_message="任务已提交，等待执行",
    )
    db.add(task)
    db.flush()
    write_event(db, task, "task.created", {"task_type": task_type, "title": task.title})
    return task


def start_task(db: Session, task: Task) -> bool:
    """任务启动：queued/pending → running（幂等：已 running 返回 True）。

    同时记录 started_at：耗时一律由该时间与当前时间计算，不依赖前端累加。
    """
    if task.status == "running":
        return True
    if "running" not in _TASK_TRANSITIONS.get(task.status, set()):
        return False
    task.status = "running"
    if task.started_at is None:
        task.started_at = datetime.now()
    db.flush()
    write_event(db, task, "task.started", {"started_at": task.started_at.isoformat()})
    return True


def set_task_step(db: Session, task: Task, *, step: str, message: str,
                  progress: int | None = None) -> None:
    """更新任务当前执行步骤与展示文案（由确定性代码写入，不由 LLM 生成）。

    用于异步任务向用户暴露真实进展：step 为机器可读标识，message 为面向文案。
    进度仍以阶段聚合为准，此处可显式覆盖（无阶段的任务直接给进度）。
    """
    task.current_step = (step or "")[:64]
    task.current_message = (message or "")[:500]
    if progress is not None:
        task.progress = max(0, min(100, int(progress)))
    db.flush()
    write_event(
        db, task, "task.step",
        {"step": task.current_step, "message": task.current_message, "progress": task.progress},
    )


def add_stage(
    db: Session,
    task: Task,
    *,
    stage_key: str,
    stage_name: str,
    job_id: str | None = None,
    depends_on: list[str] | None = None,
    agent_name: str | None = None,
) -> TaskStage:
    """追加任务阶段（stage_index 任务内递增；depends_on 声明前置阶段依赖）。"""
    last = (
        db.query(TaskStage)
        .filter(TaskStage.task_id == task.id)
        .order_by(TaskStage.stage_index.desc())
        .first()
    )
    stage = TaskStage(
        id=task_stage_id(),
        task_id=task.id,
        stage_index=(last.stage_index + 1) if last else 0,
        stage_key=stage_key,
        stage_name=stage_name,
        status="pending",
        progress=0,
        job_id=job_id,
        depends_on=depends_on,
        agent_name=agent_name,
    )
    db.add(stage)
    db.flush()
    write_event(
        db,
        task,
        "task.stage_added",
        {"stage_id": stage.id, "stage_key": stage_key, "stage_name": stage_name, "job_id": job_id},
    )
    return stage


def start_stage(db: Session, task: Task, stage: TaskStage) -> bool:
    if stage.status == "running":
        return True
    if "running" not in _STAGE_TRANSITIONS.get(stage.status, set()):
        return False
    # 依赖门禁（Phase 5）：前置阶段未全部 succeeded 时不可启动（等待人工审核/前置完成）
    if stage.depends_on:
        deps = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
        by_key = {d.stage_key: d for d in deps}
        for dep_key in stage.depends_on:
            dep = by_key.get(dep_key)
            if dep is None or dep.status != "succeeded":
                write_event(
                    db, task, "task.stage_blocked",
                    {"stage_id": stage.id, "stage_key": stage.stage_key,
                     "blocked_by": dep_key, "dep_status": dep.status if dep else "missing"},
                )
                return False
    stage.status = "running"
    _refresh_task_running(db, task)
    db.flush()
    write_event(db, task, "task.stage_started", {"stage_id": stage.id, "stage_key": stage.stage_key})
    return True


def update_stage_progress(db: Session, task: Task, stage: TaskStage, progress: int) -> None:
    """更新阶段进度（0-100 截断），并聚合任务进度。"""
    stage.progress = max(0, min(100, int(progress)))
    _aggregate_progress(db, task)
    db.flush()
    write_event(
        db,
        task,
        "task.stage_progress",
        {"stage_id": stage.id, "stage_key": stage.stage_key, "progress": stage.progress, "task_progress": task.progress},
    )


def finish_stage(
    db: Session,
    task: Task,
    stage: TaskStage,
    status: str,
    result: dict | None = None,
) -> bool:
    """阶段终态流转：succeeded / failed / skipped / cancelled（failed 可重试回 running）。"""
    if status not in _STAGE_TRANSITIONS.get(stage.status, set()):
        return False
    stage.status = status
    if result is not None:
        stage.result = result
    if status == "succeeded":
        stage.progress = 100
    _aggregate_progress(db, task)
    db.flush()
    write_event(
        db,
        task,
        f"task.stage_{status}",
        {"stage_id": stage.id, "stage_key": stage.stage_key, "result": result},
    )
    # Phase 2 失败级联：阶段失败时跳过依赖它的后置阶段
    if status == "failed":
        _skip_dependent_stages(db, task, stage)
    # Phase 2 自动调度：任一阶段终态后尝试启动下一批就绪阶段
    schedule_ready_stages(db, task)
    return True


def complete_task(db: Session, task: Task, *, summary: str, result_data: dict | None = None) -> bool:
    """任务完成：写终态事件与结构化结果卡片。"""
    if "completed" not in _TASK_TRANSITIONS.get(task.status, set()):
        return False
    task.status = "completed"
    task.progress = 100
    if result_data is not None:
        db.add(
            TaskResult(
                id=task_result_id(),
                task_id=task.id,
                result_type=result_data.get("result_type", "summary"),
                data=result_data,
            )
        )
    db.flush()
    write_event(db, task, "task.finished", {"summary": summary[:2000]})
    return True


def fail_task(db: Session, task: Task, *, error_code: str, error_msg: str) -> bool:
    if "failed" not in _TASK_TRANSITIONS.get(task.status, set()):
        return False
    task.status = "failed"
    task.error_code = error_code[:48]
    task.error_msg = error_msg[:4000]
    db.flush()
    write_event(db, task, "task.failed", {"error_code": task.error_code, "error_msg": task.error_msg})
    return True


def cancel_task(db: Session, task: Task, reason: str | None = None) -> bool:
    if "cancelled" not in _TASK_TRANSITIONS.get(task.status, set()):
        return False
    # Phase 2 取消传播：将所有非终态阶段标记为 cancelled
    stages = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
    for s in stages:
        if s.status in ("pending", "running"):
            s.status = "cancelled"
            write_event(
                db,
                task,
                "task.stage_cancelled",
                {"stage_id": s.id, "stage_key": s.stage_key, "reason": reason},
            )
    task.status = "cancelled"
    db.flush()
    write_event(db, task, "task.cancelled", {"reason": reason})
    return True


# ---------- Job 事件桥接：job_event → task_event ----------

# Job 状态 → 阶段状态映射
_JOB_STATUS_TO_STAGE = {
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


def bridge_job_event(
    db: Session,
    *,
    job_id: str,
    event_type: str,
    payload: dict | None = None,
) -> int:
    """把 Job 事件桥接为 Task 事件（按 task_stage.job_id 找归属任务）。

    - job.started → task.stage_started（阶段 running）
    - job.finished + job.status=succeeded → task.stage_succeeded
    - job.finished + job.status=failed → task.stage_failed
    - job.retry / job.quality_retry → task.stage_progress（透传进度信息）
    返回桥接的事件数（0 表示该 Job 不属于任何任务）。
    """
    stages = db.query(TaskStage).filter(TaskStage.job_id == job_id).all()
    if not stages:
        return 0
    bridged = 0
    for stage in stages:
        task = db.get(Task, stage.task_id)
        if task is None:
            continue
        if event_type == "job.started":
            start_stage(db, task, stage)
        elif event_type == "job.finished":
            job_status = (payload or {}).get("status")
            if not job_status:
                job = db.get(Job, job_id)
                job_status = getattr(job, "status", None) if job else None
            stage_status = _JOB_STATUS_TO_STAGE.get(job_status or "succeeded", "succeeded")
            finish_stage(db, task, stage, stage_status, result=payload)
        elif event_type in ("job.retry", "job.quality_retry"):
            write_event(
                db,
                task,
                "task.stage_progress",
                {"stage_id": stage.id, "stage_key": stage.stage_key, "source": event_type, **(payload or {})},
            )
            # 同步回写上层 AgentTask：failed → running，保持两层状态一致
            _sync_agent_task_retry(db, task, event_type)
        else:
            write_event(
                db,
                task,
                "task.job_event",
                {"stage_id": stage.id, "stage_key": stage.stage_key, "source": event_type, **(payload or {})},
            )
        bridged += 1
        # Phase 2 自动调度：Job 状态变更后尝试启动下一批就绪阶段
        schedule_ready_stages(db, task)
        # 全阶段终态时自动闭环任务（阶段结果作为任务结果卡片数据）
        _auto_close_task_if_done(db, task)
    return bridged


def _sync_agent_task_retry(db: Session, task: Task, source: str) -> None:
    """job.retry / job.quality_retry 时同步回写上层 AgentTask（failed → running）。
    - 仅当统一 Task 关联 agent_task_id 且上层任务处于 failed 时回写
    - 局部导入避免 task_orchestrator 与 parent_agent 循环依赖
    """
    if not task.agent_task_id:
        return
    from app.models.agent import AgentTask
    from app.services.parent_agent import retry_task
    agent_task = db.get(AgentTask, task.agent_task_id)
    if agent_task is None or agent_task.status != "failed":
        return
    retry_task(db, agent_task, note="agent task retried by " + source)


def _auto_close_task_if_done(db: Session, task: Task) -> None:
    """所有阶段到达终态时闭环任务：全 succeeded → completed（写结果卡片）；任一 failed → failed。"""
    if task.status not in ("queued", "pending", "running"):
        return
    stages = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
    if not stages or any(s.status in ("pending", "running") for s in stages):
        return
    if any(s.status == "failed" for s in stages):
        failed = next(s for s in stages if s.status == "failed")
        fail_task(
            db, task,
            error_code=(failed.result or {}).get("error_code", "stage_failed"),
            error_msg=str((failed.result or {}).get("error_msg") or failed.stage_name)[:2000],
        )
    else:
        last_ok = next((s for s in reversed(stages) if s.status == "succeeded" and s.result), None)
        result_data = None
        summary = "任务完成"
        if last_ok is not None and isinstance(last_ok.result, dict):
            r = dict(last_ok.result)
            # job.finished payload 的 result 为嵌套字段：解包为卡片数据
            inner = r.get("result")
            if isinstance(inner, dict):
                r = {**{k: v for k, v in r.items() if k not in ("status", "result")}, **inner}
            result_data = {"result_type": "shots", **{k: v for k, v in r.items() if k != "status"}}
            summary = f"解析完成：{r.get('shots', 0)} 个镜头"
        complete_task(db, task, summary=summary, result_data=result_data)


def set_task_step_by_job(db: Session, job_id: str, *, step: str, message: str,
                         progress: int | None = None) -> int:
    """按 job_id 更新归属任务的当前步骤与文案（执行器里程碑用），返回更新数。

    解析等异步任务的执行器只有 job_id，无法直接持有 Task 对象；
    该入口让执行器把『正在读取剧本 / 正在解析结构 / 正在归一化 / 正在落库』
    这类真实阶段写到任务上，供对话与任务卡片读取（不模拟进度）。
    """
    stages = db.query(TaskStage).filter(TaskStage.job_id == job_id).all()
    seen: set[str] = set()
    for stage in stages:
        if stage.task_id in seen:
            continue
        seen.add(stage.task_id)
        task = db.get(Task, stage.task_id)
        if task is None:
            continue
        set_task_step(db, task, step=step, message=message, progress=progress)
    return len(seen)


def update_stage_progress_by_job(db: Session, job_id: str, progress: int) -> int:
    """按 job_id 更新关联阶段进度（执行器里程碑进度用），返回更新数。"""
    stages = db.query(TaskStage).filter(TaskStage.job_id == job_id).all()
    for stage in stages:
        task = db.get(Task, stage.task_id)
        if task is None:
            continue
        update_stage_progress(db, task, stage, progress)
    return len(stages)


# ---------- 查询辅助 ----------


def task_payload(db: Session, task: Task) -> dict:
    """任务卡片数据（chat 端点 payload / 前端任务卡片渲染）。"""
    stages = (
        db.query(TaskStage)
        .filter(TaskStage.task_id == task.id)
        .order_by(TaskStage.stage_index)
        .all()
    )
    result = (
        db.query(TaskResult)
        .filter(TaskResult.task_id == task.id)
        .order_by(TaskResult.created_at.desc())
        .first()
    )
    # 耗时一律由后端 started_at 推导，前端不得自行累加
    base = task.started_at or task.created_at
    elapsed = 0
    if base is not None:
        if base.tzinfo is not None:
            base = base.replace(tzinfo=None)
        end = task.updated_at or datetime.now()
        if end.tzinfo is not None:
            end = end.replace(tzinfo=None)
        elapsed = max(0, int((end - base).total_seconds()))
    return {
        "task_id": task.id,
        "project_id": task.project_id,
        "conversation_id": task.session_id,
        "message_id": task.message_id,
        "task_type": task.task_type,
        "title": task.title,
        "status": task.status,
        "progress": task.progress,
        "current_step": task.current_step,
        "current_message": task.current_message,
        "started_at": base.isoformat() if base is not None else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "elapsed_seconds": elapsed,
        "error_code": task.error_code,
        "error_msg": task.error_msg,
        "stages": [
            {
                "stage_id": s.id,
                "stage_index": s.stage_index,
                "stage_key": s.stage_key,
                "stage_name": s.stage_name,
                "status": s.status,
                "progress": s.progress,
                "job_id": s.job_id,
            }
            for s in stages
        ],
        "result": result.data if result else None,
    }


# ---------- 内部 ----------


def _aggregate_progress(db: Session, task: Task) -> None:
    """任务进度 = 各阶段进度均值（无阶段时保持原值）。"""
    stages = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
    if not stages:
        return
    task.progress = round(sum(s.progress for s in stages) / len(stages))


def _refresh_task_running(db: Session, task: Task) -> None:
    """任一阶段 running 时任务应为 running（幂等）。

    queued 与历史 pending 同为『已受理待执行』，阶段真正启动时都必须推进为 running，
    否则任务卡片会一直停在『等待执行』。
    """
    if task.status in ("queued", "pending"):
        task.status = "running"
        if task.started_at is None:
            task.started_at = datetime.now()


def bridge_approval_resolved(db: Session, *, confirmation_id: str, approve: bool, comment: str | None = None) -> int:
    """审批裁决事件桥接：按 agent_task_id 关联的统一 Task 写 task.approval_resolved 事件。

    返回桥接的事件数（0 表示该审批无关联统一 Task）。
    """
    from app.models.agent import AgentConfirmation
    confirmation = db.get(AgentConfirmation, confirmation_id)
    if confirmation is None:
        return 0
    tasks = db.query(Task).filter(Task.agent_task_id == confirmation.task_id).all()
    bridged = 0
    for task in tasks:
        write_event(
            db, task, "task.approval_resolved",
            {"confirmation_id": confirmation_id, "approve": approve, "comment": comment},
        )
        bridged += 1
    return bridged


def reconcile_orphan_tasks(db: Session) -> int:
    """恢复对账（Phase 7）：worker 重启/桥接丢失后闭环孤儿统一 Task。

    规则：
    - pending/running 的 Task，其阶段关联的 Job 已全部到终态（succeeded/failed/cancelled）
      但 Task 未闭环（桥接丢失）：按 Job 终态闭环（succeeded → completed + 结果卡片；failed → failed）
    - 任一 Job 仍 queued/running（含恢复重置回 queued 的续跑任务）：保持 Task 现状，等待事件桥接
    - 无阶段或阶段无 Job 关联的 Task：不处理（由业务侧显式闭环）
    返回对账闭环数。
    """
    closed = 0
    candidates = db.query(Task).filter(Task.status.in_(["pending", "running"])).all()
    for task in candidates:
        stages = db.query(TaskStage).filter(TaskStage.task_id == task.id).all()
        if not stages:
            continue
        job_ids = [s.job_id for s in stages if s.job_id]
        if not job_ids:
            continue
        jobs = db.query(Job).filter(Job.id.in_(job_ids)).all()
        if not jobs:
            continue
        # 任一 Job 未到终态：等待事件桥接，不动
        if any(j.status in ("queued", "running") for j in jobs):
            continue
        # 同步阶段状态（桥接丢失补偿）
        job_by_id = {j.id: j for j in jobs}
        for stage in stages:
            if stage.job_id and stage.status in ("pending", "running"):
                job = job_by_id.get(stage.job_id)
                if job is None:
                    continue
                stage_status = _JOB_STATUS_TO_STAGE.get(job.status, "succeeded")
                finish_stage(db, task, stage, stage_status, result=job.result)
        if any(j.status == "failed" for j in jobs):
            failed_job = next(j for j in jobs if j.status == "failed")
            fail_task(
                db, task,
                error_code=failed_job.error_code or "job_failed",
                error_msg=(failed_job.error_msg or "")[:2000],
            )
        else:
            last_ok = next((j for j in reversed(jobs) if j.result), None)
            result_data = None
            summary = "任务完成（恢复对账闭环）"
            if last_ok is not None and isinstance(last_ok.result, dict):
                result_data = {"result_type": "shots", **last_ok.result}
                summary = f"解析完成：{last_ok.result.get('shots', 0)} 个镜头"
            complete_task(db, task, summary=summary, result_data=result_data)
        closed += 1
    if closed:
        db.commit()
    return closed
