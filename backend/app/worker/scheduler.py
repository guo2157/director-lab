"""Worker 调度主循环（D-03 修订：并发 = 可用 ComfyUI 实例数）。
- 数据库表 + 单 worker 进程轮询，不引入外部消息队列（D-02）
- 启动时恢复：扫描 running/submission_unknown 任务，按 ARCHITECTURE.md §5.4 决定续跑
- 并发规则（D-18 修订 D-03）：
  * Mock 模式 / 单实例：串行执行，同一时刻只执行一个任务（与旧行为一致）
  * 多实例：并发 = 启用实例数，任务由实例池加权轮询分发（负载均衡），
    单实例同一时刻至多执行一个任务（实例侧 ComfyUI 本身为串行队列）
- 调度模型：主循环为唯一取任务者（无取任务竞态），执行线程池按并发度消费；
  线程崩溃残留的 running 任务由下次启动恢复逻辑重置
"""
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.integrations import comfyui_pool
from app.models import Job
from app.worker.compose_executor import execute_compose_job
from app.worker.executor import execute_generate_job
from app.worker.export_executor import execute_export_job
from app.worker.parse_executor import execute_parse_job

_RUNNING = True


def _handle_signal(signum, frame):
    global _RUNNING
    _RUNNING = False


def _reconcile_submission_unknown(db: Session) -> int:
    """submission_unknown 自动对账（D-23，§5.4 优先续跑）：
    - 查远端 history：有结果 → 置回 queued 续跑（executor 会走 submission_unknown 分支续跑下载）
    - 无结果且队列中无该 prompt_id → 确认未提交成功，重置 queued 安全重提
    - 无结果但仍在远端队列 → 保持 submission_unknown，下轮启动再对账
    返回对账处理数。"""
    from app.integrations.comfyui import ComfyUIClient, ComfyUIError

    reconciled = 0
    unknown_jobs = db.query(Job).filter(
        Job.status == "submission_unknown", Job.prompt_id.isnot(None)
    ).all()
    for job in unknown_jobs:
        snapshot = job.input_snapshot or {}
        base_url = snapshot.get("comfyui_instance")
        if not base_url:
            continue
        try:
            client = ComfyUIClient(base_url=base_url, timeout=10.0)
            history = client.get_history(job.prompt_id)
        except (ComfyUIError, Exception):
            continue  # 实例不可达：保持现状，下轮再对账
        if history is not None:
            # 远端有结果：保持 submission_unknown，executor 续跑分支直接下载产物（D-23 修复）
            # 不置回 queued，避免重新提交浪费算力
            reconciled += 1
            continue
        # 远端无结果：检查是否仍在队列中
        try:
            queue = client.get_queue()
        except (ComfyUIError, Exception):
            continue
        in_queue = any(
            item and len(item) > 1 and item[1] == job.prompt_id
            for item in (queue.get("queue_pending", []) + queue.get("queue_running", []))
        )
        if not in_queue:
            # 确认未提交成功：重置 queued 安全重提
            job.status = "queued"
            job.prompt_id = None
            job.submission_state = "not_submitted"
            reconciled += 1
    db.commit()
    return reconciled


def _reconcile_comfy_timeout_jobs(db: Session) -> int:
    """comfy_timeout/comfy_unreachable 任务对账：本地等待超时或实例失联判失败，
    但远端 ComfyUI 并不会停止渲染（失联多为瞬时网络抖动/实例过载）。
    对 failed 且 error_code in (comfy_timeout, comfy_unreachable)、带 prompt_id 的任务，
    按提交时绑定的实例（input_snapshot.comfyui_instance）查远端 history；
    发现已成功完成则重置为 queued 并打 resume_prompt 标记，由 executor 续跑分支
    直接下载产物（不重复提交、不浪费算力）。
    返回对账处理数。"""
    from app.integrations.comfyui import ComfyUIClient, ComfyUIError
    reconciled = 0
    timeout_jobs = db.query(Job).filter(
        Job.status == "failed",
        Job.error_code.in_(("comfy_timeout", "comfy_unreachable")),
        Job.prompt_id.isnot(None),
    ).all()
    for job in timeout_jobs:
        snapshot = job.input_snapshot or {}
        base_url = snapshot.get("comfyui_instance")
        if not base_url:
            continue
        try:
            client = ComfyUIClient(base_url=base_url, timeout=10.0)
            history = client.get_history(job.prompt_id)
        except (ComfyUIError, Exception):
            continue  # 实例不可达：保持现状，下轮再对账
        if history is None:
            continue
        try:
            done, err = client.is_job_done(history)
        except Exception:
            continue
        if not done or err:
            continue
        # 远端已成功完成：重置为 queued 续跑（executor 走续跑分支直接下载产物）
        job.status = "queued"
        job.error_code = None
        job.error_msg = None
        job.finished_at = None
        job.submission_state = "submitted"
        _snap = dict(job.input_snapshot or {})
        _snap["resume_prompt"] = True
        job.input_snapshot = _snap
        reconciled += 1
    if reconciled:
        db.commit()
    return reconciled


def _recover_interrupted_jobs(db: Session) -> int:
    """启动恢复：将 worker 宕机时残留的 running/submission_unknown 任务重置为 queued 续跑。
    规则（§5.4）：优先续跑，确认远端无该任务后再重跑；此处采用保守策略——
    running 重置为 queued；submission_unknown 交由 _reconcile_submission_unknown 自动对账。
    """
    recovered = 0
    stale = db.query(Job).filter(Job.status == "running").all()
    for job in stale:
        job.status = "queued"
        recovered += 1
    db.commit()
    return recovered


def _recover_stalled_llm_jobs(db: Session, active_job_ids: set) -> int:
    """运行期自愈：回收长时间无进展的 LLM 任务（script_agent / parse）。

    背景：LLM 流式中途停摆时任务线程可能永久阻塞，串行队列被堵死。
    llm.py 的流式空闲超时是第一道防线，此处做第二道兜底：
    running 且 started_at 早于 llm_job_stall_seconds 之前、且本进程没有对应执行线程的任务，
    按重试上限决定重新入队（未达上限）或标记失败（已达上限）。
    """
    settings = get_settings()
    stall_seconds = float(getattr(settings, "llm_job_stall_seconds", 300.0) or 300.0)
    max_attempts = int(getattr(settings, "job_max_attempts", 3) or 3)
    deadline = datetime.now() - timedelta(seconds=stall_seconds)
    stale = (
        db.query(Job)
        .filter(
            Job.status == "running",
            Job.job_type.in_(("script_agent", "parse")),
            Job.started_at.isnot(None),
            Job.started_at < deadline,
        )
        .all()
    )
    recovered = 0
    for job in stale:
        if job.id in active_job_ids:
            continue
        if (job.attempts or 0) < max_attempts:
            job.status = "queued"
        else:
            job.status = "failed"
            job.error_code = "job_stalled"
            job.error_msg = f"任务运行超过 {stall_seconds:.0f} 秒无进展，已达重试上限"
            job.finished_at = datetime.now()
        recovered += 1
    if recovered:
        db.commit()
    return recovered


def _next_job(db: Session) -> Job | None:
    """按优先级取下一个 queued 任务：priority 降序，同优先级按入队顺序（先进先出）。
    重试任务（attempts>0）在调度时提升优先级（+100），保障失败任务尽快重跑（D-22）。"""
    from sqlalchemy import case
    return (
        db.query(Job)
        .filter(Job.status == "queued")
        .order_by(
            (Job.priority + case((Job.attempts > 0, 100), else_=0)).desc(),
            Job.created_at,
        )
        .first()
    )


def _compute_concurrency() -> int:
    """计算执行并发度：Mock 模式固定 1；否则为启用 ComfyUI 实例数（至少 1）。
    实例配置运行时生效，每轮调度前重算（实例增减后无需重启 worker）。"""
    settings = get_settings()
    if settings.render_mock:
        return 1
    try:
        return max(1, len(comfyui_pool.enabled_instances()))
    except Exception:
        return 1


_GENERATE_TYPES = {"generate_keyframe", "generate_clip", "generate_audio", "generate_bgm", "generate_sfx"}


def _execute_job(job_id: str, job_type: str) -> dict:
    """在工作线程中执行单个任务（每线程独立数据库会话由执行器内部创建）。"""
    if job_type in _GENERATE_TYPES:
        return execute_generate_job(job_id)
    if job_type == "script_agent":
        from app.services.script_agent import execute_script_agent_job
        return execute_script_agent_job(job_id)
    if job_type == "parse":
        return execute_parse_job(job_id)
    if job_type == "compose":
        return execute_compose_job(job_id)
    if job_type == "export":
        return execute_export_job(job_id)
    # 未支持类型：直接置失败
    db = SessionLocal()
    try:
        j = db.get(Job, job_id)
        if j is not None and j.status == "queued":
            j.status = "failed"
            j.error_code = "unsupported_job_type"
            j.error_msg = f"任务类型 {job_type} 的执行器尚未接入"
            db.commit()
    finally:
        db.close()
    return {"ok": False, "reason": "unsupported_job_type"}


def run_worker(once: bool = False) -> None:
    """启动 worker 主循环。once=True 时只执行一轮（用于测试，等价于旧串行行为）。"""
    global _RUNNING
    settings = get_settings()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    db = SessionLocal()
    recovered = _recover_interrupted_jobs(db)
    if recovered:
        print(f"[worker] 恢复 {recovered} 个中断任务为 queued")
    reconciled = _reconcile_submission_unknown(db)
    if reconciled:
        print(f"[worker] 对账 {reconciled} 个 submission_unknown 任务")
    resumed = _reconcile_comfy_timeout_jobs(db)
    if resumed:
        print(f"[worker] 对账取回 {resumed} 个远端已完成的 comfy_timeout 任务")
    db.close()
    # 启动 ComfyUI 实例健康心跳（熔断机制后台探测，幂等）
    comfyui_pool.start_heartbeat()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dlab-worker")
    # future -> job_id 映射：运行期失联回收时据此排除本进程正在执行的任务
    in_flight: dict = {}
    last_stall_check = 0.0
    try:
        while _RUNNING:
            # 每轮重算并发度（实例配置运行时生效）；线程池按需扩容
            concurrency = _compute_concurrency()
            if pool._max_workers != concurrency:  # noqa: SLF001
                pool.shutdown(wait=False)
                pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dlab-worker")
                # 旧线程池的在途任务仍保留在 in_flight，避免被失联回收误判
            # 回收已完成任务
            in_flight = {f: jid for f, jid in in_flight.items() if not f.done()}
            db = SessionLocal()
            try:
                # 运行期自愈（节流 30 秒）：LLM 类任务长时间无进展且本进程无执行线程时重新入队
                now_ts = time.monotonic()
                if now_ts - last_stall_check >= 30.0:
                    last_stall_check = now_ts
                    stalled = _recover_stalled_llm_jobs(db, set(in_flight.values()))
                    if stalled:
                        print(f"[worker] 回收 {stalled} 个长时间无进展的 LLM 任务为 queued")
                    resumed = _reconcile_comfy_timeout_jobs(db)
                    if resumed:
                        print(f"[worker] 对账取回 {resumed} 个远端已完成的 comfy_timeout 任务")
                job = _next_job(db)
            finally:
                db.close()
            if job is None:
                if once:
                    break
                time.sleep(settings.worker_poll_interval_seconds)
                continue
            print(f"[worker] 调度任务 {job.id} ({job.job_type})，并发={concurrency}，在途={len(in_flight)}")
            fut = pool.submit(_execute_job, job.id, job.job_type)
            in_flight[fut] = job.id
            if once:
                # 测试模式：等待该任务完成后结束（保持旧串行语义）
                fut.result()
                break
            # 并发已满时等待任一任务完成再继续取任务，避免任务在内存中堆积。
            # 注意：该等待段可能持续数分钟（视频生成耗时），必须在此继续执行周期性对账，
            # 否则只要有一个任务在跑，comfy_timeout/comfy_unreachable 任务的取回就会停摆。
            while len(in_flight) >= concurrency and _RUNNING:
                time.sleep(0.2)
                in_flight = {f: jid for f, jid in in_flight.items() if not f.done()}
                now_ts = time.monotonic()
                if now_ts - last_stall_check >= 30.0:
                    last_stall_check = now_ts
                    db2 = SessionLocal()
                    try:
                        resumed = _reconcile_comfy_timeout_jobs(db2)
                        if resumed:
                            print(f"[worker] 对账取回 {resumed} 个远端已完成的 comfy_timeout 任务")
                    except Exception as _re:
                        print(f"[worker] 等待期对账异常: {type(_re).__name__}: {_re}")
                    finally:
                        db2.close()
        # 优雅停止：等待在途任务完成
        for f in in_flight:
            try:
                f.result()
            except Exception as e:
                print(f"[worker] 任务线程异常: {e}")
    finally:
        pool.shutdown(wait=True)
    print("[worker] 停止")


if __name__ == "__main__":
    run_worker()
