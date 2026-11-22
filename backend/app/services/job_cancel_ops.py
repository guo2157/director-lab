"""批量取消任务：**唯一权威实现**。

为什么单独抽一个模块
------------------
「取消项目下所有在途任务」这件事原本只在 project_purge 里做过一次（项目 force 删除）。
分镜图 / 分镜看板还需要「只取消本阶段的在途任务」（批量出图跑到一半想改工作流重来），
如果两处各写一份，必然像以往那样分叉：一边记得调 ComfyUI /interrupt、一边忘了清
prompt_id，或者一边同步了编排层阶段、另一边留下永远 running 的 Task。

所以取消动作只在这里实现一份，参数不同、逻辑同一套：

- 项目删除：``cancel_jobs`` 传入整个项目的在途 Job
- 分镜页批量取消：``select_inflight`` 按 job_type（keyframe / clip / audio）筛
- 未来任何「取消」入口（单条 / 批量 / 按镜头）都应该走这里

取消一条在途任务必须做全五件事，缺任何一件都会留下不一致状态：
1. running 的生成任务调 ComfyUI ``/interrupt`` —— 远端不认识本地状态，不中断会继续占卡跑完
2. 清 ``prompt_id`` —— 否则 scheduler 续跑分支会复用一个已取消的远端任务
3. 置 ``status=cancelled`` + ``finished_at`` —— executor 轮询 DB 状态时据此退出
4. 写 ``job.cancelled`` 事件并桥接编排层（task_stage → cancelled → Task 自动闭环），
   否则任务卡片会永远停在「执行中」
5. 对 running 任务登记跨进程取消信号（``job_cancel.request``），
   让阻塞在 socket read 上的线程立刻退出
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Job, JobEvent

#: 视为「仍在进行」的任务状态（与删除预检、进度口径保持一致）
BLOCKING_JOB_STATUSES = ("queued", "running", "submission_unknown", "chained_wait")

#: 只有真正提交到远端的生成类任务才需要 /interrupt 释放 GPU
GENERATE_JOB_TYPES = {
    "generate_keyframe",
    "generate_clip",
    "generate_audio",
    "generate_bgm",
    "generate_sfx",
}

#: 分镜页批量取消的 target → job_type 映射（None 表示不按类型过滤）
TARGET_JOB_TYPES: dict[str, tuple[str, ...] | None] = {
    "keyframe": ("generate_keyframe",),
    "clip": ("generate_clip",),
    "audio": ("generate_audio", "generate_bgm", "generate_sfx"),
    "all": None,
}


def _cancel_orphan_tasks(db: Session, cancelled_job_ids: list[str], *, reason: str) -> int:
    """把「已无任何在途 Job」的上游编排任务置 cancelled，返回任务数。

    这类任务继续留在 running 只会让调度器继续派发 pending 阶段，
    取消动作就白做了；同时任务卡片停在「执行中」也是一种谎报。
    """
    from app.models import TaskStage
    from app.services import task_orchestrator

    task_ids = {
        r[0]
        for r in db.query(TaskStage.task_id)
        .filter(TaskStage.job_id.in_(cancelled_job_ids))
        .distinct()
        .all()
        if r[0]
    }
    if not task_ids:
        return 0

    # 任务名下仍在途的 Job（同任务其它阶段可能正在跑，不能整体取消）
    still_inflight = {
        r[0]
        for r in db.query(TaskStage.task_id)
        .join(Job, Job.id == TaskStage.job_id)
        .filter(
            TaskStage.task_id.in_(task_ids),
            Job.status.in_(BLOCKING_JOB_STATUSES),
        )
        .distinct()
        .all()
        if r[0]
    }

    cancelled = 0
    for tid in task_ids - still_inflight:
        task = db.query(task_orchestrator.Task).filter(task_orchestrator.Task.id == tid).first()
        if task is None or task.status not in ("pending", "queued", "running"):
            continue
        try:
            if task_orchestrator.cancel_task(db, task, reason=reason):
                cancelled += 1
        except Exception:  # noqa: BLE001
            pass
    if cancelled:
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return cancelled


def select_inflight(
    db: Session,
    project_id: str,
    *,
    target: str = "all",
    shot_ids: list[str] | None = None,
) -> list[Job]:
    """按项目 + 目标类型（+ 可选镜头）筛出在途任务。未知 target 抛 ValueError。"""
    if target not in TARGET_JOB_TYPES:
        raise ValueError(target)
    q = db.query(Job).filter(
        Job.project_id == project_id, Job.status.in_(BLOCKING_JOB_STATUSES)
    )
    job_types = TARGET_JOB_TYPES[target]
    if job_types is not None:
        q = q.filter(Job.job_type.in_(job_types))
    if shot_ids:
        q = q.filter(Job.shot_id.in_(shot_ids))
    return q.all()


def cancel_jobs(db: Session, jobs: list[Job], *, reason: str) -> dict:
    """取消给定任务（只处理仍在途的），返回取消回执。调用方负责筛选。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cancelled_ids: list[str] = []
    running_ids: list[str] = []
    interrupted = 0

    for j in jobs:
        if j.status not in BLOCKING_JOB_STATUSES:
            continue
        if j.status == "running" and j.job_type in GENERATE_JOB_TYPES and j.prompt_id:
            try:
                from app.integrations.comfyui import ComfyUIClient

                used_url = (j.input_snapshot or {}).get("comfyui_instance")
                if used_url:
                    ComfyUIClient(base_url=used_url).interrupt()
                else:
                    ComfyUIClient().interrupt()
                interrupted += 1
            except Exception as e:  # noqa: BLE001  中断失败不阻断本地取消
                db.add(
                    JobEvent(
                        job_id=j.id,
                        event_type="job.interrupt_failed",
                        payload={"reason": type(e).__name__},
                        created_at=now,
                    )
                )
        was_running = j.status == "running"
        j.status = "cancelled"
        j.finished_at = now
        j.prompt_id = None  # 防止 scheduler 续跑已取消的远端任务
        db.add(
            JobEvent(
                job_id=j.id,
                event_type="job.cancelled",
                payload={"reason": reason},
                created_at=now,
            )
        )
        cancelled_ids.append(j.id)
        if was_running:
            running_ids.append(j.id)

    if cancelled_ids:
        db.commit()

    # 编排层同步：job.finished + status=cancelled → task_stage 置 cancelled，
    # 全部阶段终态时 Task 自动闭环（否则任务卡片永远停在「执行中」）。
    from app.services.task_orchestrator import bridge_job_event

    for jid in cancelled_ids:
        try:
            with db.begin_nested():
                bridge_job_event(
                    db, job_id=jid, event_type="job.finished", payload={"status": "cancelled"}
                )
        except Exception:  # noqa: BLE001  桥接失败不影响取消本身
            pass
    if cancelled_ids:
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    # 上游任务收尾：只取消「已经没有在途 Job」的任务。
    #
    # 为什么必须做：bridge_job_event 末尾会调 schedule_ready_stages，
    # 把还没派发的 pending 阶段立刻启动 —— 用户点了「取消在途」，
    # 下一秒调度器又把下一阶段发出去了（新 Job 重新排队），取消等于没做。
    # 只在任务确实没有其它在途 Job 时才取消，避免误杀同时在跑别的阶段的任务。
    cancelled_tasks = 0
    if cancelled_ids:
        cancelled_tasks = _cancel_orphan_tasks(db, cancelled_ids, reason=reason)

    # 跨进程取消信号：同进程立刻掐断在途流，跨进程由 worker 轮询 DB 感知
    if running_ids:
        try:
            from app.services import job_cancel

            for jid in running_ids:
                job_cancel.request(jid)
        except Exception:  # noqa: BLE001
            pass

    return {
        "jobs": len(cancelled_ids),
        "running": len(running_ids),
        "interrupted": interrupted,
        "tasks": cancelled_tasks,
        "job_ids": cancelled_ids,
    }
