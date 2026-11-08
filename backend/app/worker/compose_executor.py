"""合成任务执行器：PRD.md §6.7 M7 合成规则的完整生命周期。
流程：收集可合成镜头与产物 → 构建时间轴（含合成前校验）→ 逐镜对齐 → 拼接
     → BGM 混音/响度归一 → 字幕 → 合成记录落库。
纪律：
- FFmpeg/ffprobe 等媒体操作在短数据库事务之外（ARCHITECTURE.md §5.1）
- 事件与状态同事务写入 outbox
- 失败按 §5.5 分类记录 error_code
- 产物路径来自资产表 file_path；工作目录用系统临时目录，产物写 media_dir
"""
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Asset, AssetRef, Composition, Job, JobEvent, Project, Scene, Shot
from app.models import ids

logger = logging.getLogger(__name__)
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
    # 统一任务编排层桥接：job_event → task_event（Job 归属任务时同步任务阶段状态）
    try:
        from app.services.task_orchestrator import bridge_job_event
        bridge_job_event(db, job_id=job.id, event_type=event_type, payload=payload or {})
    except Exception:  # noqa: BLE001
        pass  # 桥接失败不影响既有执行链路




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
    asset_type 匹配 + 来源任务 succeeded，二者缺一不可；
    asset_ref 只用于关联镜头与资产，不单独作为产物具备依据。
    """
    from app.models import Job as JobModel
    q = (
        db.query(Asset)
        .join(JobModel, JobModel.id == Asset.source_job_id)
        .filter(JobModel.shot_id == shot_id,
                Asset.asset_type.in_(asset_types),
                JobModel.status == "succeeded")
    )
    if job_type is not None:
        q = q.filter(JobModel.job_type == job_type)
    return q.order_by(Asset.version.desc()).first()


def _shot_start_ms(db: Session, shot: Shot) -> int:
    """镜头在本集时间轴上的起始毫秒（累计前面镜头的计划时长）。

    Shot.seq 是场景内镜号，不能单独比较：必须先比 Scene.seq 再比 Shot.seq，
    否则会把其他场景中镜号更小的镜头时长错误累计进来（跨场景串镜）。
    """
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return 0
    rows = db.execute(
        select(Shot.duration)
        .join(Scene, Scene.id == Shot.scene_id)
        .where(
            Scene.project_id == scene.project_id,
            Shot.episode == shot.episode,
            or_(
                Scene.seq < scene.seq,
                and_(Scene.seq == scene.seq, Shot.seq < shot.seq),
            ),
        )
    ).scalars().all()
    return int(round(sum(float(d or 0) for d in rows) * 1000))


def _collect_tts_audios(db: Session, shot: Shot, stats: dict | None = None) -> list[dict]:
    """收集镜头可混入的 TTS 对白/旁白片段（discard 策略混音输入）。

    - track_type ∈ (dialogue, narration)，generation_status=completed，audio_asset_id 非空
    - **仅混入 review_status=approved 的片段**（方案要求：审核通过后才可用于最终合成）；
      未审核/已驳回片段计入 stats['pending_review'] 并在合成结果 warnings 中提示
    - 定位规则：优先用片段 start_time_ms（相对本集时间线）换算为镜头内 offset，
      仅当该字段缺失时才按 sequence 顺序累加；两者混用会导致有真实停顿的片段被压实
    - 重叠检测：offset 早于前一片段结束时间时计入 stats['overlap']（真实重叠需人工确认）
    - 跨镜旁白：cross_shot=True 且 shot_id 为空、start_time_ms 落在本镜头区间内的旁白
      纳入本镜混音；跨出镜头末尾被 atrim 截断的部分计入 stats['cross_shot_clipped']
    - duration_ms 缺失时跳过并计入 stats['no_duration']（时长未知无法定位）
    """
    from sqlalchemy import and_, or_
    from app.models.audio import AudioSegment
    shot_start_ms = _shot_start_ms(db, shot)
    shot_end_ms = shot_start_ms + int(round(float(shot.duration or 0) * 1000))
    segs = db.execute(
        select(AudioSegment).where(
            AudioSegment.track_type.in_(("dialogue", "narration")),
            AudioSegment.generation_status == "completed",
            AudioSegment.audio_asset_id.isnot(None),
            or_(
                AudioSegment.shot_id == shot.id,
                and_(AudioSegment.shot_id.is_(None),
                     AudioSegment.cross_shot.is_(True),
                     AudioSegment.episode == shot.episode,
                     AudioSegment.start_time_ms.isnot(None),
                     AudioSegment.start_time_ms >= shot_start_ms,
                     AudioSegment.start_time_ms < shot_end_ms),
            ),
        ).order_by(AudioSegment.sequence, AudioSegment.created_at)
    ).scalars().all()
    audios: list[dict] = []
    cursor_ms = 0
    prev_end_ms = 0
    for seg in segs:
        if seg.review_status != "approved":
            if stats is not None:
                stats["pending_review"] = stats.get("pending_review", 0) + 1
            continue
        if not seg.duration_ms or seg.duration_ms <= 0:
            if stats is not None:
                stats["no_duration"] = stats.get("no_duration", 0) + 1
            continue
        asset = db.get(Asset, seg.audio_asset_id)
        if asset is None or not asset.file_path:
            if stats is not None:
                stats["asset_missing"] = stats.get("asset_missing", 0) + 1
            continue
        # 定位：优先绝对时间（start_time_ms 相对本集），缺失才顺序累加
        explicit = seg.start_time_ms is not None and seg.start_time_ms >= 0
        offset_ms = (int(seg.start_time_ms) - shot_start_ms) if explicit else cursor_ms
        if offset_ms < 0:
            offset_ms = 0
        if offset_ms < prev_end_ms and stats is not None:
            stats["overlap"] = stats.get("overlap", 0) + 1
        if seg.cross_shot and offset_ms + int(seg.duration_ms) > shot_end_ms - shot_start_ms:
            if stats is not None:
                stats["cross_shot_clipped"] = stats.get("cross_shot_clipped", 0) + 1
        audios.append({
            "segment_id": seg.id,
            "track_type": seg.track_type,
            "path": asset.file_path,
            "offset_ms": offset_ms,
            "duration_ms": seg.duration_ms,
            "asset_id": asset.id,
            "asset_version": asset.version,
            "explicit_start": explicit,
            "cross_shot": bool(seg.cross_shot),
        })
        cursor_ms = offset_ms + int(seg.duration_ms)
        prev_end_ms = cursor_ms
    return audios


def _to_shot_input(db: Session, shot: Shot, scene: Scene, audio_policy: str = "discard",
                   tts_enabled: bool = True, stats: dict | None = None) -> cs.ShotInput:
    """把镜头与其产物整理为合成输入结构。

    音频来源决策（V1 解耦方案，按项目级 native_audio_policy）：
    - discard：镜头有已生成 TTS 片段 → tts_mix（丢弃内嵌音轨，TTS 按时间轴混入）；
      无 TTS 片段 → 保持原 audio_source 但丢弃内嵌音轨（drop_native_audio）
    - preserve：h3_va 内嵌音轨保留为底层；若同时存在已审核通过的 TTS 片段且 tts_enabled，
      则转为 tts_mix 并置 keep_native_audio=True，内嵌音轨作底层与 TTS 叠加（不再丢弃配音）
    - native：优先保留 h3_va 内嵌音轨（口型与内嵌语音同步，适合 H3 ref_audio 生成）；
      无内嵌音轨的镜头才回退 TTS 混音，避免整片静音
    - preserve_ambience_only：V1 无可靠人声分离能力，按 discard 处理（方案要求分离失败回退 discard）
    """
    # 视频产物：clip 类型、当前有效、来源任务成功（§4.2 口径）
    v_asset = _latest_asset(db, shot.id, ("clip",))
    a_asset = None
    audio_source = shot.audio_source
    tts_audios: list[dict] = []
    drop_native = False
    keep_native = False
    if shot.audio_source == "tts_fallback":
        a_asset = _latest_asset(db, shot.id, ("audio",), job_type="generate_audio")
    elif audio_policy in ("discard", "preserve_ambience_only"):
        if tts_enabled:
            tts_audios = _collect_tts_audios(db, shot, stats=stats)
        if tts_audios:
            audio_source = "tts_mix"
        else:
            # 无可用 TTS 片段（未生成/未审核通过/TTS 已关闭）：丢弃内嵌音轨（discard），
            # 最终音轨由 BGM/SFX/响度归一链路处理
            drop_native = True
    elif audio_policy == "native":
        # 原生音轨优先：H3 内嵌语音与口型同步，直接使用内嵌音轨，不用 TTS 覆盖；
        # 无内嵌音轨的镜头才回退 TTS 混音
        if shot.audio_source != "h3_va":
            if tts_enabled:
                tts_audios = _collect_tts_audios(db, shot, stats=stats)
            if tts_audios:
                audio_source = "tts_mix"
            else:
                drop_native = True
    elif audio_policy == "preserve" and tts_enabled:
        # 问题4 修复：preserve 策略此前完全不收集 TTS，已生成且审核通过的配音被静默丢弃。
        # 现行为：有内嵌音轨（h3_va）→ 保留为底层，TTS 叠加其上；无内嵌音轨 → 纯 TTS 混音。
        tts_audios = _collect_tts_audios(db, shot, stats=stats)
        if tts_audios:
            audio_source = "tts_mix"
            keep_native = shot.audio_source == "h3_va"
    return cs.ShotInput(
        shot_id=shot.id, episode=shot.episode, seq=shot.seq, scene_seq=scene.seq,
        duration=shot.duration, actual_duration=shot.actual_duration,
        audio_source=audio_source, status=shot.status, dialogue=shot.dialogue,
        video_path=(v_asset.file_path if v_asset else None),
        video_asset_id=(v_asset.id if v_asset else None),
        video_asset_version=(v_asset.version if v_asset else None),
        audio_path=(a_asset.file_path if a_asset else None),
        audio_asset_id=(a_asset.id if a_asset else None),
        audio_asset_version=(a_asset.version if a_asset else None),
        review_released=(shot.status == "done"),
        tts_audios=tts_audios,
        drop_native_audio=drop_native,
        keep_native_audio=keep_native,
    )


def _task_progress(db: Session, job_id: str, progress: int) -> None:
    """统一任务编排层：按 job_id 更新关联任务阶段进度（里程碑推进，异常隔离）。"""
    try:
        from app.services.task_orchestrator import update_stage_progress_by_job
        update_stage_progress_by_job(db, job_id, progress)
    except Exception:  # noqa: BLE001
        pass


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
        # 项目级音频策略（默认 native：使用 H3 内嵌音轨，TTS 只作 ref_audio 参考）
        audio_cfg = ((project.settings or {}) if project else {}).get("audio_config") or {}
        audio_policy = audio_cfg.get("native_audio_policy", "native")
        # 修复（问题1）：三个开关此前只在 UI 存在、合成链路从未读取，属假开关。
        tts_enabled = bool(audio_cfg.get("tts_enabled", True))
        bgm_enabled = bool(audio_cfg.get("bgm_enabled", True))
        sfx_enabled = bool(audio_cfg.get("sfx_enabled", True))

        # 收集镜头与产物（只读查询，短事务）
        rows = _collect_shots(db, job.project_id, episode)
        _task_progress(db, job.id, 30)
        if not rows:
            _fail_job(db, job, "output_missing", "项目内没有可合成的镜头（须为 done/review 状态）")
            return {"ok": False, "reason": "no_shots"}
        # TTS 片段收集统计（未审核/缺时长/缺资产），用于合成结果告警
        tts_stats: dict = {}
        shot_inputs = [
            _to_shot_input(db, shot, scene, audio_policy=audio_policy,
                           tts_enabled=tts_enabled, stats=tts_stats)
            for shot, scene in rows
        ]

        # BGM 资产（bgm_enabled=false 时不混入，即使任务传了 bgm_asset_id）
        bgm_path = None
        if bgm_asset_id and bgm_enabled:
            bgm = db.get(Asset, bgm_asset_id)
            if bgm and bgm.file_path:
                bgm_path = bgm.file_path
        elif bgm_asset_id and not bgm_enabled:
            logger.info("项目音频策略禁用 BGM，忽略 bgm_asset_id=%s", bgm_asset_id)

        # 构建时间轴（含 ffprobe 与合成前校验，媒体操作在事务外）
        timeline = cs.build_timeline(shot_inputs, tolerance=tolerance)
        # 修复（问题2）：TTS 片段仅混入已审核通过的，被跳过的必须让用户看得见
        if tts_stats.get("pending_review"):
            timeline.warnings.append(
                f"{tts_stats['pending_review']} 条对白/旁白未审核通过，未混入成片（需在镜头音频面板审核）")
        if tts_stats.get("no_duration"):
            timeline.warnings.append(f"{tts_stats['no_duration']} 条音频时长未知，未混入成片")
        if tts_stats.get("asset_missing"):
            timeline.warnings.append(f"{tts_stats['asset_missing']} 条音频资产文件缺失，未混入成片")
        if tts_stats.get("overlap"):
            timeline.warnings.append(
                f"{tts_stats['overlap']} 条音频与前一条时间重叠，混音可能相互盖压，建议核对 start_time_ms")
        if tts_stats.get("cross_shot_clipped"):
            timeline.warnings.append(
                f"{tts_stats['cross_shot_clipped']} 条跨镜旁白超出本镜时长被截断，需按整集时间轴复核")
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
            degraded_shots = []
            for i, item in enumerate(timeline.items):
                seg_path = os.path.join(workdir, f"seg_{i:04d}.mp4")
                try:
                    cs.run_cmd(cs.shot_segment_cmd(item, seg_path, width=width, height=height))
                except Exception as seg_err:  # noqa: BLE001
                    # 问题10 修复：单镜混音失败（坏音频/解码异常）不应拖垮整集合成。
                    # 降级为静音轨重试一次并记录告警，便于事后单独重生成该镜配音。
                    from dataclasses import replace as _dc_replace
                    degraded = _dc_replace(
                        item.shot, tts_audios=[], keep_native_audio=False,
                        drop_native_audio=True,
                        audio_source="none" if item.shot.audio_source == "tts_mix"
                        else item.shot.audio_source,
                    )
                    retry_item = _dc_replace(item, shot=degraded)
                    logger.warning("镜头 ep%s/seq%s 片段合成失败，降级为静音重试: %s",
                                   item.shot.episode, item.shot.seq, seg_err)
                    cs.run_cmd(cs.shot_segment_cmd(retry_item, seg_path,
                                                   width=width, height=height))
                    degraded_shots.append(f"ep{item.shot.episode}/seq{item.shot.seq}")
                segments.append(seg_path)
            if degraded_shots:
                timeline.warnings.append(
                    "以下镜头音频混音失败已降级为静音，请重生成对应配音后重新合成："
                    + "、".join(degraded_shots))
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
            # 字幕：sidecar 输出 SRT 外挂；burn 生成 ASS 后由 libass 烧入画面
            srt_rel = None
            final = mixed
            if include_subtitle:
                if subtitle_mode == "sidecar":
                    srt_text = cs.generate_srt(timeline)
                    if srt_text.strip():
                        srt_dest = media_dir / f"{comp_id}.srt"
                        srt_dest.write_text(srt_text, encoding="utf-8")
                        srt_rel = str(srt_dest)
                else:
                    ass_text = cs.build_ass(
                        timeline, width=width, height=height,
                        font=settings.subtitle_font or None,
                    )
                    if "Dialogue:" in ass_text:
                        ass_path = os.path.join(workdir, f"{comp_id}.ass")
                        with open(ass_path, "w", encoding="utf-8") as f:
                            f.write(ass_text)
                        burned = os.path.join(workdir, "burned.mp4")
                        cs.run_cmd(cs.burn_subtitle_cmd(mixed, ass_path, burned))
                        final = burned
                        timeline.warnings.append("字幕已烧录（按镜头台词生成 ASS）")
                    else:
                        timeline.warnings.append("镜头均无台词，未生成字幕")
            # 产物落盘
            final_path = media_dir / f"{comp_id}.mp4"
            with open(final, "rb") as src, open(final_path, "wb") as dst:
                dst.write(src.read())
            duration = cs.probe_duration(str(final_path))
            # 渲染后自检（post-render check）：时长一致 / 文件非空 / 音轨存在性；
            # 校验失败不标记 succeeded，避免交付坏成片（验证独立于合成动作）
            if final_path.stat().st_size <= 0:
                raise RuntimeError("成片文件为空")
            if duration <= 0:
                raise RuntimeError("成片时长探测无效")
            if abs(duration - timeline.total_duration) > max(tolerance, 1.0):
                raise RuntimeError(
                    f"成片时长 {duration:.2f}s 与时间轴 {timeline.total_duration:.2f}s 偏差超限"
                )
            if any(item.shot.audio_asset_id for item in timeline.items):
                if not cs.probe_has_audio(str(final_path)):
                    raise RuntimeError("镜头含配音但成片无音轨")
        except Exception as e:
            _fail_job(db, job, "comfy_execution_error", f"FFmpeg 合成失败: {e}")
            return {"ok": False, "reason": "ffmpeg_error"}

        # 合成记录落库（source_snapshot 按集分组）
        # 问题5 修复：tts_mix 镜头必须记录参与混音的每条 TTS 片段 ID 与资产版本，
        # 否则成片不可追溯（无法判断某条配音是否进了这一版成片、用的是哪个音频版本）。
        snapshot: dict = {}
        for item in timeline.items:
            ep_key = str(item.shot.episode)
            snapshot.setdefault(ep_key, []).append({
                "shot_id": item.shot.shot_id,
                "video_asset_id": item.shot.video_asset_id,
                "video_asset_version": item.shot.video_asset_version,
                "audio_asset_id": item.shot.audio_asset_id,
                "audio_asset_version": item.shot.audio_asset_version,
                "audio_source": item.shot.audio_source,
                "keep_native_audio": bool(item.shot.keep_native_audio),
                "tts_segments": [
                    {
                        "segment_id": ta.get("segment_id"),
                        "track_type": ta.get("track_type"),
                        "asset_id": ta.get("asset_id"),
                        "asset_version": ta.get("asset_version"),
                        "offset_ms": ta.get("offset_ms"),
                        "duration_ms": ta.get("duration_ms"),
                    }
                    for ta in (item.shot.tts_audios or [])
                ],
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
        _task_progress(db, job.id, 60)
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded",
                                               "result": {"composition_id": comp_id, "duration": duration}})
        db.commit()

        # 跨集连续性：合成成功后自动提取末帧（对应改造第四节）
        try:
            from app.services.continuity import extract_episode_last_frame, invalidate_downstream_continuity
            if episode is not None:
                media_dir = Path(settings.media_dir)
                # 先使下游连续性资产失效（版本递增），再提取当前集末帧
                invalidated = invalidate_downstream_continuity(db, job.project_id, episode)
                if invalidated:
                    logger.info("第 %d 集重合成，已使 %d 个下游剧集连续性资产失效", episode, len(invalidated))
                extract_episode_last_frame(db, job.project_id, episode, comp, media_dir)
                logger.info("第 %d 集末帧提取完成", episode)
        except Exception as e:
            logger.warning("末帧提取失败（不影响合成结果）: %s", e)

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
