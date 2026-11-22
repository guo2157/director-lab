"""任务查询接口：API.md §任务 契约。
- GET /api/jobs：任务列表（分页 page/page_size + 增量补齐 updated_since_seq）
- GET /api/jobs/stats：任务统计（今日成功/失败、进行中、近 7 日平均耗时）
- GET /api/jobs/{id}：任务详情，含 error_code/error_msg/retryable/result/seq
- POST /api/jobs/{id}/retry：重试（新任务 ID，继承累计次数与 input_snapshot）
- POST /api/jobs/{id}/cancel：取消（running 生成任务调用 ComfyUI 中断）
- POST /api/jobs/projects/{id}/cancel：批量取消在途任务（target=keyframe/clip/audio/all）
- POST /api/jobs/projects/{id}/retry-failed：批量重试（D-14）
retryable 判定：failed/lost 且 attempts < job_max_attempts（§5.7）。
SSE 事件流见 events.py（/api/events）。
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import Job, JobEvent, Project, Scene, Shot
from app.schemas.script import JobOut
from app.services import errors

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_RETRYABLE_STATUS = ("failed", "lost")


def _to_out(j: Job, db: Session | None = None) -> JobOut:
    settings = get_settings()
    retryable = j.status in _RETRYABLE_STATUS and (j.attempts or 0) < settings.job_max_attempts
    project_name = episode = scene_title = shot_seq = None
    progress = None
    progress_chars = None
    if db is not None:
        project = db.get(Project, j.project_id)
        project_name = project.name if project else None
        if j.shot_id:
            shot = db.get(Shot, j.shot_id)
            if shot:
                shot_seq = shot.seq
                episode = shot.episode
                scene = db.get(Scene, shot.scene_id)
                scene_title = scene.title if scene else None
        # 生成进度百分比：running 任务取最近 job.progress 事件（value/max）换算 0-100。
        # max<=0 表示「总量未知、只有绝对推进量」（如 LLM 流式输出的已接收字符数）：
        # 此时不换算百分比，原样把已接收量放进 progress_chars，前端显示字符数推进。
        if j.status == "running":
            ev = (
                db.query(JobEvent)
                .filter(JobEvent.job_id == j.id, JobEvent.event_type == "job.progress")
                .order_by(JobEvent.event_seq.desc())
                .first()
            )
            if ev is not None:
                value = (ev.payload or {}).get("value")
                maxv = (ev.payload or {}).get("max")
                if isinstance(value, (int, float)) and isinstance(maxv, (int, float)) and maxv > 0:
                    progress = max(0, min(100, round(value / maxv * 100)))
                elif isinstance(value, (int, float)):
                    progress_chars = int(value)
    return JobOut(
        id=j.id, project_id=j.project_id, job_type=j.job_type, status=j.status, shot_id=j.shot_id,
        project_name=project_name, episode=episode, scene_title=scene_title, shot_seq=shot_seq,
        error_code=j.error_code, error_msg=j.error_msg, retryable=retryable,
        result=j.result, attempts=j.attempts or 0, seq=j.event_seq, progress=progress,
        progress_chars=progress_chars,
        created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
    )


@router.get("")
def list_jobs(project_id: str | None = Query(None), status: str | None = Query(None),
              job_type: str | None = Query(None), limit: int = Query(50, ge=1, le=200),
              page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              updated_since_seq: int | None = Query(None, ge=0),
              updated_since: datetime | None = Query(None),
              db: Session = Depends(get_db)) -> dict:
    q = db.query(Job)
    if project_id:
        q = q.filter(Job.project_id == project_id)
    if status:
        q = q.filter(Job.status == status)
    if job_type:
        q = q.filter(Job.job_type == job_type)
    # 增量补齐（API.md §9/§12）：updated_since_seq 为准（seq 严格大于，升序）；
    # updated_since 时间戳仅兼容保留，同一毫秒内多条更新可能漏取或重复
    if updated_since_seq is not None:
        q = q.filter(Job.event_seq > updated_since_seq)
        total = q.count()
        items = q.order_by(Job.event_seq.asc()).limit(limit).all()
        return {"total": total, "items": [_to_out(j, db).model_dump(mode="json") for j in items]}
    if updated_since is not None:
        q = q.filter(Job.created_at >= updated_since)
    total = q.count()
    # 分页：page/page_size 优先（API.md §9 契约），limit 作为兼容参数
    if page_size:
        items = q.order_by(Job.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    else:
        items = q.order_by(Job.created_at.desc()).limit(limit).all()
    return {"total": total, "items": [_to_out(j, db).model_dump(mode="json") for j in items]}


@router.get("/stats")
def job_stats(project_id: str | None = Query(None), db: Session = Depends(get_db)) -> dict:
    """任务统计（API.md §任务）：今日成功/失败、进行中、近 7 日平均耗时。"""
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    q = db.query(Job)
    if project_id:
        q = q.filter(Job.project_id == project_id)
    today_succeeded = q.filter(Job.status == "succeeded", Job.finished_at >= today_start).count()
    today_failed = q.filter(Job.status.in_(("failed", "lost")), Job.finished_at >= today_start).count()
    in_progress = q.filter(Job.status.in_(("queued", "running"))).count()
    finished_week = q.filter(Job.status.in_(("succeeded", "failed", "lost")), Job.finished_at >= week_ago).all()
    durations = [
        (j.finished_at - j.started_at).total_seconds()
        for j in finished_week
        if j.finished_at and j.started_at
    ]
    avg_duration = round(sum(durations) / len(durations), 2) if durations else None
    return {
        "today_succeeded": today_succeeded,
        "today_failed": today_failed,
        "in_progress": in_progress,
        "avg_duration_seconds": avg_duration,
    }


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    return _to_out(j, db)


_GENERATE_TYPES = {"generate_keyframe", "generate_clip", "generate_audio", "generate_bgm", "generate_sfx"}


@router.post("/{job_id}/cancel", status_code=204)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """取消任务：queued/running/chained_wait 可取消。
    running 的生成类任务调用 ComfyUI /interrupt（全局中断，§5.2）；
    chained_wait 尚未提交远端，仅本地置 cancelled（链式改并行重提场景）；
    其余 running 任务标记取消，worker 在下一检查点确认终态。
    """
    from datetime import datetime, timezone
    from app.models import JobEvent
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    if j.status not in ("queued", "running", "chained_wait"):
        raise errors.invalid_state_transition(f"任务状态 {j.status} 不可取消")
    if j.status == "running" and j.job_type in _GENERATE_TYPES and j.prompt_id:
        try:
            from app.integrations.comfyui import ComfyUIClient
            # 多实例：中断任务实际使用的实例（input_snapshot.comfyui_instance）
            used_url = (j.input_snapshot or {}).get("comfyui_instance")
            ComfyUIClient(base_url=used_url).interrupt() if used_url else ComfyUIClient().interrupt()
        except Exception as e:  # 中断失败不阻断本地取消，记入事件
            db.add(JobEvent(job_id=j.id, event_type="job.interrupt_failed",
                            payload={"reason": type(e).__name__},
                            created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    was_running = j.status == "running"
    j.status = "cancelled"
    j.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(JobEvent(job_id=j.id, event_type="job.cancelled", payload={},
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    # 协作式取消：仅 running 需要置进程内信号。queued 的改数据库状态就够了
    # （调度器只捞 queued，不会再有线程接手），置信号反而会留下永不清理的登记。
    # running 时线程正阻塞在 socket read 上，只改数据库它感知不到
    # （实测会跑完 24 分钟并把 status 覆写回 succeeded）。
    if was_running:
        from app.services import job_cancel
        job_cancel.request(j.id)
    return None


@router.post("/{job_id}/retry", status_code=201)
def retry_job(job_id: str, db: Session = Depends(get_db)) -> JobOut:
    """重试任务：仅 failed/lost 且 retryable 可重试，生成新的任务 ID（§任务）。"""
    from datetime import datetime, timezone
    from app.models import ids
    settings = get_settings()
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    if j.status not in ("failed", "lost"):
        raise errors.invalid_state_transition(f"任务状态 {j.status} 不可重试")
    if (j.attempts or 0) >= settings.job_max_attempts:
        raise errors.invalid_request(f"已达重试上限 {settings.job_max_attempts} 次，不可再重试")
    # 在途任务去重：同镜头同目标已有 queued/running 任务时拒绝重试，避免产生重复任务
    if j.shot_id:
        dup = (
            db.query(Job)
            .filter(
                Job.shot_id == j.shot_id,
                Job.job_type == j.job_type,
                Job.status.in_(("queued", "running")),
            )
            .first()
        )
        if dup is not None:
            raise errors.AppError(
                "job_in_flight",
                f"该镜头已有同目标在途任务（{dup.id}，状态 {dup.status}），请等待完成或先取消",
                409,
            )
    new_job = Job(
        id=ids.job_id(), project_id=j.project_id, shot_id=j.shot_id,
        job_type=j.job_type, workflow_id=j.workflow_id, workflow_version=j.workflow_version,
        params=j.params, status="queued",
        attempts=j.attempts,  # 继承累计次数，执行器运行时再 +1
        input_snapshot=j.input_snapshot,  # 复制输入快照（含 comfyui_instance，供取消定位实例）
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(new_job)
    db.commit()
    return _to_out(new_job, db)


@router.post("/projects/{project_id}/cancel")
def cancel_project_jobs(
    project_id: str,
    target: str = Query("all", description="keyframe / clip / audio / all：只取消该阶段的在途任务"),
    shot_ids: str | None = Query(None, description="可选，逗号分隔的镜头 ID，只取消这些镜头的任务"),
    db: Session = Depends(get_db),
) -> dict:
    """批量取消项目下的在途任务（分镜图 / 分镜看板页的「取消在途」入口）。

    为什么需要这个入口（2026-11-21）：
    任务中心页面已下线，逐条取消任务的 UI 不复存在，而分镜图 / 分镜看板在批量生成后
    完全没有取消入口——批量出图跑错工作流、批量出视频排了 8 镜，用户只能等它跑完。
    本接口提供按 stage（job_type）维度的批量取消。

    取消语义与单条取消完全一致（共用 job_cancel_ops.cancel_jobs）：
    - running 生成任务调 ComfyUI /interrupt 释放显卡
    - 清 prompt_id，避免 scheduler 复跑已取消任务
    - 写 job.cancelled 事件并桥接编排层（task_stage → cancelled，Task 自动闭环）
    - 对 running 任务登记跨进程取消信号
    """
    from app.services.job_cancel_ops import TARGET_JOB_TYPES, cancel_jobs, select_inflight

    p = db.get(Project, project_id)
    if p is None or p.deleted_at is not None:
        raise errors.AppError("project_not_found", "作品不存在", 404)
    if target not in TARGET_JOB_TYPES:
        raise errors.AppError(
            "invalid_target",
            f"target 只支持 {'/'.join(TARGET_JOB_TYPES)}，收到 {target}",
            400,
        )
    ids = [s.strip() for s in (shot_ids or "").split(",") if s.strip()]
    jobs = select_inflight(db, project_id, target=target, shot_ids=ids or None)
    stats = cancel_jobs(db, jobs, reason=f"bulk_cancel_{target}")
    return {
        "project_id": project_id,
        "target": target,
        **stats,
    }


@router.post("/projects/{project_id}/retry-failed", status_code=201)
def retry_failed_jobs(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """批量重试项目内失败任务（D-14）：按状态筛选 failed/lost 且未达重试上限的任务，
    逐个创建重试任务（复用单任务重试语义：新任务 ID、继承累计次数）；
    可选 job_ids 限定范围。返回重试/跳过明细。"""
    from datetime import datetime, timezone
    from app.models import ids
    settings = get_settings()
    q = db.query(Job).filter(
        Job.project_id == project_id,
        Job.status.in_(["failed", "lost"]),
        Job.attempts < settings.job_max_attempts,
    )
    job_ids = payload.get("job_ids")
    if job_ids:
        q = q.filter(Job.id.in_(job_ids))
    retried, skipped = [], []
    # 预取在途任务（shot_id, job_type）集合，用于批量重试去重
    inflight = {
        (x.shot_id, x.job_type)
        for x in db.query(Job)
        .filter(Job.status.in_(("queued", "running")), Job.shot_id.isnot(None))
        .all()
    }
    for j in q.all():
        if j.shot_id and (j.shot_id, j.job_type) in inflight:
            skipped.append({"source_job_id": j.id, "reason": "job_in_flight"})
            continue
        new_job = Job(
            id=ids.job_id(), project_id=j.project_id, shot_id=j.shot_id,
            job_type=j.job_type, workflow_id=j.workflow_id, workflow_version=j.workflow_version,
            params=j.params, status="queued",
            attempts=j.attempts,
            input_snapshot=j.input_snapshot,  # 复制输入快照（含 comfyui_instance，供取消定位实例）
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(new_job)
        retried.append({"source_job_id": j.id, "new_job_id": new_job.id})
    # 已达上限的失败任务单独列出（前端提示不可重试原因）
    capped = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.status.in_(["failed", "lost"]),
            Job.attempts >= settings.job_max_attempts,
        )
        .all()
    )
    skipped = [j.id for j in capped]
    db.commit()
    return {
        "retried": retried,
        "retried_count": len(retried),
        "skipped_capped": skipped,
        "skipped_count": len(skipped),
    }


@router.get("/{job_id}/llm-stream")
def get_llm_stream(job_id: str, db: Session = Depends(get_db)) -> dict:
    """LLM 流式输出回放：返回该任务已累积的 agent.llm_delta 文本与最新事件 seq。
    前端中途打开解析页时先调用本接口回放历史，再从 last_seq 订阅 SSE 增量，避免重复。

    同时返回最近一次 ``agent.stage``：刷新后没有 SSE 历史可听，只靠回放拿不到
    「当前在做什么」，界面会退化成只有一个输出框。
    """
    j = db.get(Job, job_id)
    if j is None:
        raise errors.job_not_found()
    rows = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job_id, JobEvent.event_type == "agent.llm_delta")
        .order_by(JobEvent.event_seq)
        .all()
    )
    text = "".join((ev.payload or {}).get("delta") or "" for ev in rows)
    last_seq = rows[-1].event_seq if rows else 0
    stage_ev = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job_id, JobEvent.event_type == "agent.stage")
        .order_by(JobEvent.event_seq.desc())
        .first()
    )
    stage = None
    if stage_ev is not None and stage_ev.payload:
        stage = {
            "stage": (stage_ev.payload or {}).get("stage"),
            "label": (stage_ev.payload or {}).get("label"),
        }
        last_seq = max(last_seq, stage_ev.event_seq)
    return {"job_id": job_id, "text": text, "last_seq": last_seq,
            "chunk_count": len(rows), "stage": stage}
