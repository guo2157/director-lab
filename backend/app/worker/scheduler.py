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


def _recover_interrupted_jobs(db: Session) -> int:
    """启动恢复：将 worker 宕机时残留的 running/submission_unknown 任务重置为 queued 续跑。
    规则（§5.4）：优先续跑，确认远端无该任务后再重跑；此处采用保守策略——
    有 prompt_id 的 submission_unknown 保留待对账（不自动重提），running 重置为 queued。
    """
    recovered = 0
    stale = db.query(Job).filter(Job.status == "running").all()
    for job in stale:
        job.status = "queued"
        recovered += 1
    db.commit()
    return recovered


def _next_job(db: Session) -> Job | None:
    """按入队顺序取下一个 queued 任务（先进先出）。"""
    return (
        db.query(Job)
        .filter(Job.status == "queued")
        .order_by(Job.created_at)
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
    db.close()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dlab-worker")
    in_flight: set = set()
    try:
        while _RUNNING:
            # 每轮重算并发度（实例配置运行时生效）；线程池按需扩容
            concurrency = _compute_concurrency()
            if pool._max_workers != concurrency:  # noqa: SLF001
                pool.shutdown(wait=False)
                pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="dlab-worker")
                in_flight = set()
            # 回收已完成任务
            in_flight = {f for f in in_flight if not f.done()}
            db = SessionLocal()
            try:
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
            in_flight.add(fut)
            if once:
                # 测试模式：等待该任务完成后结束（保持旧串行语义）
                fut.result()
                break
            # 并发已满时等待任一任务完成再继续取任务，避免任务在内存中堆积
            while len(in_flight) >= concurrency and _RUNNING:
                time.sleep(0.2)
                in_flight = {f for f in in_flight if not f.done()}
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
