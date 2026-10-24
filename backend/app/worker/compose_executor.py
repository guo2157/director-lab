"""合成任务执行器：PRD.md §6.7 M7 合成规则的完整生命周期。
流程：收集可合成镜头与产物 → 构建时间轴（含合成前校验）→ 逐镜对齐 → 拼接
     → BGM 混音/响度归一 → 字幕 → 合成记录落库。
纪律：
- FFmpeg/ffprobe 等媒体操作在短数据库事务之外（ARCHITECTURE.md §5.1）
- 事件与状态同事务写入 outbox
- 失败按 §5.5 分类记录 error_code
- 产物路径来自资产表 file_path；工作目录用系统临时目录，产物写 media_dir
"""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Asset, AssetRef, Composition, Job, JobEvent, Project, Scene, Shot
from app.models import ids
from app.services import compose as cs


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _collect_shots(db: Session, project_id: str, episode: int | None) -> list[Shot]:
    """收集可合成镜头：状态 done 或 review（review 仅人工放行可过校验），按集/场景/镜号排序。"""
    q = (
        db.query(Shot, Scene)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .filter(Shot.status.in_(["done", "review"]))
    )
    if episode is not None:
        q = q.filter(Shot.episode == episode)
    rows = q.order_by(Shot.episode, Scene.seq, Shot.seq).all()
    return rows


def _latest_asset(db: Session, shot_id: str, asset_types: tuple[str, ...], job_type: str | None = None):
    """取镜头指定类型、当前有效的最新版本资产。

    判定口径（ARCHITECTURE.md §4.2，与 progress.py 一致）：
    asset_type 匹配 + validity=current + 来源任务 succeeded，三者缺一不可；
    asset_ref 只用于关联镜头与资产，不单独作为产物具备依据。
    """
    from app.models import Job as JobModel
    q = (
        db.query(Asset)
        .join(JobModel, JobModel.id == Asset.source_job_id)
        .filter(JobModel.shot_id == shot_id,
                Asset.validity == "current", Asset.asset_type.in_(asset_types),
                JobModel.status == "succeeded")
    )
    if job_type is not None:
        q = q.filter(JobModel.job_type == job_type)
    return q.order_by(Asset.version.desc()).first()


def _to_shot_input(db: Session, shot: Shot, scene: Scene) -> cs.ShotInput:
    """把镜头与其产物整理为合成输入结构。"""
    # 视频产物：clip 类型、当前有效、来源任务成功（§4.2 口径）
    v_asset = _latest_asset(db, shot.id, ("clip",))
    a_asset = None
    if shot.audio_source == "tts_fallback":
        a_asset = _latest_asset(db, shot.id, ("audio",), job_type="generate_audio")
    return cs.ShotInput(
        shot_id=shot.id, episode=shot.episode, seq=shot.seq, scene_seq=scene.seq,
        duration=shot.duration, actual_duration=shot.actual_duration,
        audio_source=shot.audio_source, status=shot.status, dialogue=shot.dialogue,
        video_path=(v_asset.file_path if v_asset else None),
        video_asset_id=(v_asset.id if v_asset else None),
        video_asset_version=(v_asset.version if v_asset else None),
        audio_path=(a_asset.file_path if a_asset else None),
        audio_asset_id=(a_asset.id if a_asset else None),
        audio_asset_version=(a_asset.version if a_asset else None),
        review_released=(shot.status == "done"),
    )


def execute_compose_job(job_id: str) -> dict:
    """执行一个合成任务。返回执行结果摘要。"""
    settings = get_settings()
    db: Session = SessionLocal()
    workdir = None
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
        episode = params.get("episode")
        tolerance = float(params.get("compose_tolerance", 0.5))
        include_subtitle = bool(params.get("include_subtitle", True))
        subtitle_mode = params.get("subtitle_mode", "burn")
        bgm_asset_id = params.get("bgm_asset_id")
        bgm_volume = float(params.get("bgm_volume", 0.3))

        project = db.get(Project, job.project_id)
        aspect = project.aspect_ratio if project else None
        width, height = cs.resolution_for(aspect)

        # 收集镜头与产物（只读查询，短事务）
        rows = _collect_shots(db, job.project_id, episode)
        if not rows:
            _fail_job(db, job, "output_missing", "项目内没有可合成的镜头（须为 done/review 状态）")
            return {"ok": False, "reason": "no_shots"}
        shot_inputs = [_to_shot_input(db, shot, scene) for shot, scene in rows]

        # BGM 资产
        bgm_path = None
        if bgm_asset_id:
            bgm = db.get(Asset, bgm_asset_id)
            if bgm and bgm.file_path and bgm.validity == "current":
                bgm_path = bgm.file_path

        # 构建时间轴（含 ffprobe 与合成前校验，媒体操作在事务外）
        timeline = cs.build_timeline(shot_inputs, tolerance=tolerance)
        if timeline.errors:
            code, msg = timeline.errors[0]
            detail = "；".join(m for _, m in timeline.errors[:5])
            _fail_job(db, job, code, f"合成前校验未通过: {detail}")
            return {"ok": False, "reason": code}

        # FFmpeg 合成（媒体操作，事务外）
        media_dir = Path(settings.media_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        comp_id = ids.composition_id()
        workdir = tempfile.mkdtemp(prefix=f"compose_{job.id[-6:]}_")
        try:
            segments = []
            for i, item in enumerate(timeline.items):
                seg_path = os.path.join(workdir, f"seg_{i:04d}.mp4")
                cs.run_cmd(cs.shot_segment_cmd(item, seg_path, width=width, height=height))
                segments.append(seg_path)
            list_file = os.path.join(workdir, "concat.txt")
            with open(list_file, "w") as f:
                for sp in segments:
                    f.write(f"file '{sp}'\n")
            joined = os.path.join(workdir, "joined.mp4")
            cs.run_cmd(cs.concat_cmd(list_file, joined))
            # BGM 混音（如有）
            if bgm_path:
                mixed = os.path.join(workdir, "mixed.mp4")
                cs.run_cmd(cs.bgm_mix_cmd(joined, bgm_path, mixed,
                                          total_duration=timeline.total_duration,
                                          volume=bgm_volume))
                timeline.warnings.append(
                    f"BGM 混音完成：音量 {bgm_volume}，结尾 1s 淡出")
            else:
                mixed = joined
            # 响度归一（两遍法：测量有效才归一，静音/极短音频跳过防 NaN）
            measured = cs.measure_loudness(mixed)
            if measured:
                normed = os.path.join(workdir, "normed.mp4")
                cs.run_cmd(cs.loudnorm_apply_cmd(mixed, normed, measured))
                mixed = normed
                timeline.warnings.append("响度归一完成：-16 LUFS / -1 dBTP（两遍法）")
            else:
                timeline.warnings.append("音频静音或响度测量无效，跳过响度归一")
            # 字幕
            srt_rel = None
            final = mixed
            if include_subtitle:
                srt_text = cs.generate_srt(timeline)
                if srt_text.strip():
                    srt_path = os.path.join(workdir, f"{comp_id}.srt")
                    with open(srt_path, "w", encoding="utf-8") as f:
                        f.write(srt_text)
                    if subtitle_mode == "sidecar":
                        srt_dest = media_dir / f"{comp_id}.srt"
                        srt_dest.write_text(srt_text, encoding="utf-8")
                        srt_rel = str(srt_dest)
                    else:
                        burned = os.path.join(workdir, "burned.mp4")
                        cs.run_cmd(cs.burn_subtitle_cmd(mixed, srt_path, burned))
                        final = burned
            # 产物落盘
            final_path = media_dir / f"{comp_id}.mp4"
            with open(final, "rb") as src, open(final_path, "wb") as dst:
                dst.write(src.read())
            duration = cs.probe_duration(str(final_path))
        except Exception as e:
            _fail_job(db, job, "comfy_execution_error", f"FFmpeg 合成失败: {e}")
            return {"ok": False, "reason": "ffmpeg_error"}

        # 合成记录落库（source_snapshot 按集分组）
        snapshot: dict = {}
        for item in timeline.items:
            ep_key = str(item.shot.episode)
            snapshot.setdefault(ep_key, []).append({
                "shot_id": item.shot.shot_id,
                "video_asset_id": item.shot.video_asset_id,
                "video_asset_version": item.shot.video_asset_version,
                "audio_asset_id": item.shot.audio_asset_id,
                "audio_asset_version": item.shot.audio_asset_version,
                "actual_duration": item.target_duration,
                "start_time": item.start_time,
            })
        comp = Composition(
            id=comp_id, project_id=job.project_id, episode=episode,
            status="done", stale=False, file_path=str(final_path),
            duration=duration, srt_path=srt_rel, source_snapshot=snapshot,
            warnings=timeline.warnings, created_at=_now_naive(),
        )
        db.add(comp)
        job.status = "succeeded"
        job.result = {
            "composition_id": comp_id, "file_path": str(final_path),
            "duration": duration, "srt_path": srt_rel, "warnings": timeline.warnings,
        }
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded",
                                               "result": {"composition_id": comp_id, "duration": duration}})
        db.commit()
        return {"ok": True, "composition_id": comp_id, "duration": duration}
    except Exception as e:
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None and job.status not in ("succeeded", "failed"):
                _fail_job(db, job, "comfy_execution_error", f"合成执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
        if workdir and os.path.isdir(workdir):
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
