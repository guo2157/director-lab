"""导出任务执行器：API.md §10 export 契约的完整生命周期。
流程：校验合成版本（存在/done/未过期）→ 封装（standard 直接复制，high_quality 重编码高码率）
     → 导出记录落库。
纪律：
- 导出只负责交付封装，不改变 composition 状态；不隐式触发重新合成
- FFmpeg/文件操作在短数据库事务之外
- 失败按 §5.5 分类记录 error_code
"""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Composition, Export, Job, JobEvent
from app.models import ids


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = code
    job.error_msg = message[:2000]
    job.finished_at = _now()
    _write_event(db, job, "job.failed", {"error_code": code, "error_msg": job.error_msg})
    db.commit()


def execute_export_job(job_id: str) -> dict:
    """执行一个导出任务。返回执行结果摘要。"""
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status != "queued":
            return {"ok": False, "reason": f"invalid_status:{job.status}"}

        job.status = "running"
        job.started_at = _now()
        job.attempts = (job.attempts or 0) + 1
        _write_event(db, job, "job.started", {"attempt": job.attempts})
        db.commit()

        params = dict(job.params or {})
        composition_id = params.get("composition_id")
        delivery_profile = params.get("delivery_profile", "standard")
        comp = db.get(Composition, composition_id) if composition_id else None
        if comp is None or comp.project_id != job.project_id:
            _fail_job(db, job, "composition_required", "不存在成功的合成版本，需先合成")
            return {"ok": False, "reason": "composition_required"}
        if comp.stale:
            _fail_job(db, job, "composition_stale", "合成版本已失效，需重新合成后再导出")
            return {"ok": False, "reason": "composition_stale"}
        if comp.status != "done" or not comp.file_path or not os.path.exists(comp.file_path):
            _fail_job(db, job, "composition_required", "合成版本未完成或产物缺失")
            return {"ok": False, "reason": "composition_incomplete"}

        # 封装（媒体操作，事务外）
        media_dir = Path(settings.media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        export_id = ids.export_id()
        file_name = f"{job.project_id}_{export_id[-6:]}.mp4"
        out_path = media_dir / file_name
        try:
            if delivery_profile == "high_quality":
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", comp.file_path,
                     "-c:v", "libx264", "-preset", "slow", "-crf", "16",
                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)],
                    capture_output=True, text=True, timeout=1800)
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or "")[-300:])
            else:  # standard：直接封装复制，不重编码
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", comp.file_path, "-c", "copy",
                     "-movflags", "+faststart", str(out_path)],
                    capture_output=True, text=True, timeout=600)
                if proc.returncode != 0:
                    raise RuntimeError((proc.stderr or "")[-300:])
        except Exception as e:
            _fail_job(db, job, "comfy_execution_error", f"导出封装失败: {e}")
            return {"ok": False, "reason": "ffmpeg_error"}

        size = os.path.getsize(out_path)
        export = Export(
            id=export_id, project_id=job.project_id, composition_id=comp.id,
            format="mp4", delivery_profile=delivery_profile,
            file_name=file_name, size=size, status="done",
            created_at=_now().replace(tzinfo=None),
        )
        db.add(export)
        job.status = "succeeded"
        job.result = {"export_id": export_id, "file_name": file_name, "size": size,
                      "download_url": f"/api/exports/{export_id}/download"}
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded",
                                               "result": {"export_id": export_id}})
        db.commit()
        return {"ok": True, "export_id": export_id, "size": size}
    except Exception as e:
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None and job.status not in ("succeeded", "failed"):
                _fail_job(db, job, "comfy_execution_error", f"导出执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
