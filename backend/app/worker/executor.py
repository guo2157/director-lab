"""任务执行器：单个生成任务的完整生命周期。

流程：载入工作流 → 注入参数/媒体 → 上传媒体 → 提交 → 轮询 → 下载产物 → 资产落库 → 事件写入。
纪律：
- 网络与媒体处理在短数据库事务之外（AGENTS.md §3-4）
- 事件与状态同事务写入 outbox，提交前禁止发布（AGENTS.md §3-5）
- 失败按 ARCHITECTURE.md §5.5 分类记录 error_code
- 产物按 {asset_id}_{version}.{ext} 重命名落盘（§5.6）
"""
import random
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings, stored_path
from app.core import database as _db
from app.integrations.comfyui import ComfyUIClient, ComfyUIError
from app.integrations import comfyui_pool
from app.models import Asset, Character, Job, JobEvent, Project, Prop, Scene, Shot, Workflow
from app.services.workflow_inject import find_output_nodes, inject_params


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _workflow_frame_size(definition: dict | None) -> tuple[int, int] | None:
    """从工作流定义里取 H3 视频节点的 width/height。

    档位工作流（capabilities.tier 非空）自带画幅，优先于项目宽高比；
    非档位工作流返回 None，仍按 project.aspect_ratio 注入，保持原行为。
    """
    if not isinstance(definition, dict):
        return None
    for node in definition.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "MiniMaxH3ReferenceToVideo":
            continue
        inputs = node.get("inputs") or {}
        width = inputs.get("width")
        height = inputs.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
    return None


# 图片工作流画布：按项目画幅推导，像素量约 1.8MP，宽高均为 16 的倍数。
# 分镜图与场景基准图跟随项目画幅，保证与 H3 视频输出画幅一致；
# 角色定妆图固定 9:16 全身像、道具锚定图固定 1:1，避免主体被拉宽留白。
_IMAGE_CANVAS_BY_RATIO = {
    "9:16": (1024, 1792),
    "16:9": (1792, 1024),
    "1:1": (1344, 1344),
}


def _image_canvas_size(
    aspect_ratio: str | None, entity_type: str | None = None
) -> tuple[int, int]:
    """图片任务画布尺寸：实体资产按类型固定，分镜图/场景基准图跟随项目画幅。"""
    if entity_type in ("character", "asset"):
        return _IMAGE_CANVAS_BY_RATIO["9:16"]
    if entity_type == "prop":
        return _IMAGE_CANVAS_BY_RATIO["1:1"]
    return _IMAGE_CANVAS_BY_RATIO.get(
        aspect_ratio or "", _IMAGE_CANVAS_BY_RATIO["9:16"]
    )


def _group_audio_inputs(rows: list[dict], max_inputs: int = 3) -> dict:
    """把镜头音频片段按说话人分组为 H3 参考音频槽位（纯函数，便于单测）。

    规则（v2 计划 §8.6）：
    - 每个说话人只取一条：同一角色跨片段复用同一音色，双角色镜头不能把两个人的
      声音混成一条（历史故障）
    - **必须记录 character_id / track_type / segment_id**：只存文件路径的话，
      任务详情无法回答「ref_audio_2 是谁的声音」，违反「保存合并前的独立音频和角色映射」
    - 超出槽位上限时进 overflow 并显式告警，**不允许静默丢弃**
    - 总时长按全部片段累加（含未入槽的），用于帧数反算
    """
    paths: list[str] = []
    bindings: list[dict] = []
    overflow: list[dict] = []
    seen: set[str] = set()
    total_ms = 0
    for row in rows:
        total_ms += int(row.get("duration_ms") or 0)
        speaker_key = row.get("character_id") or row.get("track_type") or "unknown"
        if speaker_key in seen:
            continue
        if len(paths) >= max_inputs:
            overflow.append({
                "character_id": row.get("character_id"),
                "track_type": row.get("track_type"),
                "segment_id": row.get("segment_id"),
                "reason": "max_audio_inputs_exceeded",
            })
            continue
        seen.add(speaker_key)
        slot = f"ref_audio_{len(paths)}"
        paths.append(row["file_path"])
        bindings.append({
            "slot": slot,
            "asset_id": row.get("asset_id"),
            "character_id": row.get("character_id"),
            "track_type": row.get("track_type"),
            "segment_id": row.get("segment_id"),
            "sequence": row.get("sequence"),
            "duration_ms": int(row.get("duration_ms") or 0),
        })
    return {"paths": paths, "bindings": bindings, "overflow": overflow, "total_ms": total_ms}


def _shot_ref_audios(db: Session, shot: Shot) -> dict:
    """按说话人收集镜头内可复用配音，作为 H3 ref_audio_N 参考（v2 §8.6 / §13.5）。

    返回 {paths, bindings, overflow, total_ms, stale, legacy}：
    - paths     媒体注入用（顺序即 ref_audio_0..N）
    - bindings  写入 input_snapshot 的角色↔槽位映射，供任务详情追溯
    - overflow  因槽位不足未入槽的说话人（必须告警，不静默丢）
    - total_ms  本镜配音总时长，供 length 对齐
    - stale     因依赖指纹失配而**不可复用**的片段（调用方据此决定是否重跑 TTS）
    - legacy    A4 上线前的历史片段（无指纹）按宽限期复用，调用方必须写事件告警

    A4：复用判据从「approved 即可」升级为「approved + 指纹未变 + 文件仍在」。
    台词/音色/TTS 工作流版本/内容参数任一变化都会让指纹失配，旧音频不得注入 H3，
    否则会产出对不上台词的视频（§13.5）。
    """
    from app.models.audio import AudioSegment
    from app.services.workflow import tts_fingerprint as _fp

    segs = (
        db.query(AudioSegment)
        .filter(
            AudioSegment.shot_id == shot.id,
            AudioSegment.track_type.in_(("dialogue", "narration")),
            AudioSegment.generation_status == "completed",
            AudioSegment.review_status == "approved",
            AudioSegment.audio_asset_id.isnot(None),
        )
        .order_by(AudioSegment.sequence, AudioSegment.created_at)
        .all()
    )
    rows: list[dict] = []
    stale: list[dict] = []
    legacy: list[dict] = []
    for seg in segs:
        asset = db.get(Asset, seg.audio_asset_id)
        if asset is None or not asset.file_path:
            continue
        # 当前依赖：台词/音色/TTS 工作流（取片段当前绑定，缺失时用项目内 preset TTS 工作流）
        from app.services import tts_params as _tp
        _voice_obj = _tp.resolve_voice(db, seg)
        _voice = _voice_obj.voice_id if _voice_obj is not None else None
        _wf = _resolve_tts_workflow(db, seg)
        _params = _tp.params_for_fingerprint(seg, _voice_obj)
        decision = _fp.check_reuse(
            stored=seg.tts_fingerprint,
            text=seg.text,
            voice_id=_voice,
            tts_workflow_id=_wf.id if _wf is not None else None,
            tts_workflow_version=_wf.version if _wf is not None else None,
            tts_params=_params,
            approved=seg.review_status == "approved",
            audio_exists=bool(asset.file_path),
            # 宽限期：A4 上线前的历史片段无指纹，不阻断存量项目，但必须留痕告警
            legacy_grace=True,
        )
        if decision.legacy:
            legacy.append({
                "segment_id": seg.id,
                "character_id": seg.character_id,
                "track_type": seg.track_type,
                "reason": decision.reason,
            })
        if not decision.reusable:
            stale.append({
                "segment_id": seg.id,
                "character_id": seg.character_id,
                "track_type": seg.track_type,
                "reason": decision.reason,
                "changed": decision.changed,
            })
            continue
        rows.append({
            "file_path": asset.file_path,
            "asset_id": asset.id,
            "character_id": seg.character_id,
            "track_type": seg.track_type,
            "segment_id": seg.id,
            "sequence": seg.sequence,
            "duration_ms": seg.duration_ms,
        })
    result = _group_audio_inputs(rows)
    result["stale"] = stale
    result["legacy"] = legacy
    return result


def _resolve_tts_workflow(db: Session, seg):
    """片段音频应对应的 TTS 工作流：优先 preset 模式的 tts 工作流（与生成时路由一致）。"""
    from app.services import tts_workflow as tts_wf_service
    wf = tts_wf_service.resolve_preset_workflow(db)
    if wf is not None:
        return wf
    rows = tts_wf_service.list_tts_workflows(db)
    return rows[0] if rows else None


def _next_asset_version(
    db: Session, job: Job, asset_type: str, asset_name: str | None = None
) -> int:
    """计算产物版本号：同一归属范围内同类型取最大值加一，无历史则为 1。

    重新生成不得覆盖当前生效版本，版本号必须递增以便区分候选与正式。
    """
    q = db.query(Asset).filter(Asset.asset_type == asset_type)
    if job.shot_id:
        q = q.join(Job, Job.id == Asset.source_job_id).filter(Job.shot_id == job.shot_id)
    else:
        q = q.filter(Asset.project_id == job.project_id)
        if asset_name:
            q = q.filter(Asset.name == asset_name)
    latest = q.order_by(Asset.version.desc()).first()
    return (latest.version + 1) if latest is not None else 1


def _supersede_previous_versions(
    db: Session, job: Job, asset_type: str, asset_name: str | None = None
) -> int:
    """落库新 current 版本前，将同一归属范围内的旧 current 资产转为 superseded。

    质量重试（job.quality_retry）每次尝试都会生成新版本资产，若不废弃旧版本，
    资产库会出现同一实体的多个 current 版本。归属范围与 _next_asset_version 一致；
    无 shot 归属且无实体名时不做处理，避免误伤同类型其他资产。
    """
    if not job.shot_id and not asset_name:
        return 0
    q = db.query(Asset).filter(Asset.asset_type == asset_type, Asset.validity == "current")
    if job.shot_id:
        q = q.join(Job, Job.id == Asset.source_job_id).filter(Job.shot_id == job.shot_id)
    else:
        q = q.filter(Asset.project_id == job.project_id, Asset.name == asset_name)
    count = 0
    for old in q.all():
        old.validity = "superseded"
        count += 1
    return count


def _asset_regenerate_target(db: Session, job: Job) -> Asset | None:
    """资产库 regenerate 链路识别：input_snapshot.regeneration 为含 asset_id 的字典。

    命中时产物原地覆盖该资产文件（名称与位置固定不变），不产生新版本；
    分镜重新生成工作台的 regeneration 为布尔 True，不在此列，仍走候选版本流程。
    """
    regen = (job.input_snapshot or {}).get("regeneration")
    if not isinstance(regen, dict):
        return None
    asset_id = regen.get("asset_id")
    if not asset_id:
        return None
    return db.get(Asset, asset_id)


def _overwrite_asset_file(asset: Asset, content: bytes) -> str:
    """原地覆盖资产文件：file_path 固定不变，返回实际写入路径。

    组合参考图（character_prop_binding）没有候选版本语义：覆盖即视为生效，
    写入后把 validity 置为 current。否则 Reference Resolver 的 _valid_asset
    会因 validity=pending 永远拒绝采用，组合图能力静默失效（2026-11-06 排查确认）。
    """
    from pathlib import Path

    dest = stored_path(asset.file_path)
    if not dest.is_absolute() and not dest.exists():
        dest = Path(get_settings().media_dir) / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    asset.updated_at = _now()
    if asset.asset_type == "character_prop_binding":
        asset.validity = "current"
    return str(dest)


def _apply_regenerate_gates(db: Session, job: Job, asset: Asset) -> None:
    """资产库 regenerate 原地覆盖后的闸口（与 replace_asset 保持一致）：
    引用镜头标记待重做（失效传播），分镜图重绘后取消该镜的旧确认标记。

    人工审核状态不再回退：阶段审核机制已取消，资产变更只需让受影响的镜头
    走失效/重做流程，下一次阶段推进由就绪检查重新判定。"""
    from app.models import AssetRef

    for ref in db.query(AssetRef).filter(AssetRef.asset_id == asset.id).all():
        shot = db.get(Shot, ref.shot_id)
        if shot is None:
            continue
        if hasattr(shot, "redo_required"):
            shot.redo_required = True
        if hasattr(shot, "keyframe_confirmed"):
            shot.keyframe_confirmed = False


def _task_progress(db: Session, job_id: str, progress: int) -> None:
    """统一任务编排层：按 job_id 更新关联任务阶段进度（里程碑推进，异常隔离）。"""
    try:
        from app.services.task_orchestrator import update_stage_progress_by_job
        update_stage_progress_by_job(db, job_id, progress)
    except Exception:  # noqa: BLE001
        pass

def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    """事件与状态同事务写入 outbox。event_seq 由数据库自增，并回写 job.event_seq 供增量补齐。"""
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq
    # 统一任务编排层桥接：job_event → task_event（Job 归属任务时同步任务阶段状态）
    # 必须用 savepoint 隔离：桥接内部含写操作，若失败仅被 except 吞掉，
    # 外层 Session 会进入 PendingRollback 状态，后续 commit 抛
    # 「This Session's transaction has been rolled back due to a previous
    # exception during flush」，把已渲染成功的任务误判失败并陷入重复入队死循环。
    try:
        from app.services.task_orchestrator import bridge_job_event
        with db.begin_nested():
            bridge_job_event(db, job_id=job.id, event_type=event_type, payload=payload or {})
    except Exception:  # noqa: BLE001
        pass  # 桥接失败不影响既有执行链路；savepoint 已回滚，主事务保持干净


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
    print(f"[worker] 任务失败 {job.id} ({job.job_type}) shot={job.shot_id} code={code} msg={message[:300]}", flush=True)
    job.status = "failed"
    job.error_code = code
    job.error_msg = message[:2000]
    job.finished_at = _now()
    # 镜头状态回退：生成失败回到待生成（§3.2 未完成集合）
    if job.shot_id:
        _shot = db.get(Shot, job.shot_id)
        if _shot is not None and _shot.status == "generating":
            _shot.status = "pending"
    _write_event(db, job, "job.failed", {"error_code": code, "error_msg": job.error_msg})
    # 链式生成（chained）：前序失败后级联取消链上等待任务，避免链条永久卡死且无法重提
    try:
        _cancel_chained_after_failure(db, job, code)
    except Exception as _ce:  # noqa: BLE001
        _write_event(db, job, "chain.cancel_failed", {"error": str(_ce)[:200]})
    db.commit()


def _probe_clip_and_update_shot(db: Session, job: Job, created_assets: list[dict]) -> None:
    """clip 产物 ffprobe 检测（compose 对齐依赖）：回写 Shot 时长与音频来源。

    - actual_duration / video_actual_duration：视频实际时长（秒）
    - audio_source：存在音频流 → h3_va（H3 VA 内嵌音频），否则 none
    ffprobe 失败不阻断任务成功：记录 clip.probe_failed 事件，字段保持原值，
    由后续对账/音频补救流程处理。
    """
    if job.job_type != "generate_clip" or not job.shot_id:
        return
    clip = next((a for a in created_assets if a.get("asset_type") == "clip"), None)
    if clip is None:
        return
    import json
    import subprocess
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", clip["file_path"]],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "ffprobe failed").strip()[:200])
        info = json.loads(proc.stdout)
        streams = info.get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if not has_video:
            raise RuntimeError("no video stream")
        duration = None
        for s in streams:
            if s.get("codec_type") == "video" and s.get("duration"):
                duration = float(s["duration"])
                break
        if duration is None:
            fmt_dur = (info.get("format") or {}).get("duration")
            if fmt_dur:
                duration = float(fmt_dur)
        shot = db.get(Shot, job.shot_id)
        if shot is not None:
            if duration is not None and duration > 0:
                shot.actual_duration = duration
                shot.video_actual_duration = duration
            shot.audio_source = "h3_va" if has_audio else "none"
        _write_event(db, job, "clip.probe", {
            "shot_id": job.shot_id,
            "duration": duration,
            "audio_source": "h3_va" if has_audio else "none",
        })
        # 音频模块改造（2026-11-06）：登记/更新 H3 原生音频片段。
        # 片段绑定本次视频资产版本（clip.asset_id），视频重新生成后由本函数重建，
        # 旧片段删除、审核状态不继承；无音频流时登记 absent 状态，流程不阻断。
        try:
            from app.services import audio_plan
            if shot is not None:
                audio_plan.register_native_audio(
                    db, job.project_id, shot.episode, shot.id,
                    clip["asset_id"], has_audio,
                )
        except Exception as e:  # noqa: BLE001
            try:
                _write_event(db, job, "native_audio.register_failed", {"error": str(e)[:200]})
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        try:
            _write_event(db, job, "clip.probe_failed", {"error": str(e)[:200]})
        except Exception:
            pass


def _sync_audio_segment_after_generate(db: Session, job: Job, created_assets: list[dict]) -> None:
    """音频生成任务成功后回写片段状态（音频模块改造，2026-11-06）。

    - generate_audio / generate_bgm / generate_sfx：片段指向新资产版本，
      generation_status=completed，review_status 回到 pending（新版本不继承旧审核）
    - duration_ms 以 ffprobe 实际探测为准，探测失败不阻断
    - 片段定位：input_snapshot.audio_segment_id 优先；缺失时仅当同镜同轨道类型的待生成
      片段**唯一**才回写，多条候选时不做猜测（按 created_at 取首条会把音频挂到错误的片段上），
      改为写 job 事件留待人工确认
    """
    if job.job_type not in ("generate_audio", "generate_bgm", "generate_sfx"):
        return
    audio_asset = next((a for a in created_assets if a.get("asset_type") == "audio"), None)
    if audio_asset is None:
        return
    # 音色试听任务：回写 voice_profile 试听缓存。
    # 指纹与文本在提交时已固化到 input_snapshot，这里只做落库，
    # 避免回写时角色音色已被改动导致缓存与音频不匹配。
    _snap_preview = job.input_snapshot or {}
    if _snap_preview.get("voice_preview") and _snap_preview.get("voice_profile_id"):
        from app.models.audio import VoiceProfile as _VoiceProfile
        _vp = db.get(_VoiceProfile, _snap_preview["voice_profile_id"])
        if _vp is not None:
            _vp.preview_asset_id = audio_asset["asset_id"]
            _vp.preview_text = _snap_preview.get("preview_text")
            _vp.preview_signature = _snap_preview.get("preview_signature")
            _write_event(db, job, "audio.voice_preview_cached", {
                "voice_profile_id": _vp.id,
                "asset_id": audio_asset["asset_id"],
                "signature": _vp.preview_signature,
            })
        return
    # 系统音色试听任务：回写 voice_library 试听缓存（音色智能匹配，2026-11-07）
    if _snap_preview.get("voice_preview") and _snap_preview.get("system_voice_code"):
        from sqlalchemy import select as _sel
        from app.models.audio import VoiceLibrary as _VoiceLibrary
        _vl = db.execute(
            _sel(_VoiceLibrary).where(
                _VoiceLibrary.voice_code == _snap_preview["system_voice_code"]
            )
        ).scalars().first()
        if _vl is not None:
            _vl.preview_asset_id = audio_asset["asset_id"]
            _write_event(db, job, "audio.system_voice_preview_cached", {
                "voice_code": _vl.voice_code,
                "asset_id": audio_asset["asset_id"],
                "signature": _snap_preview.get("preview_signature"),
            })
        return
    from app.models.audio import AudioSegment
    seg = None
    seg_id = (job.input_snapshot or {}).get("audio_segment_id")
    if seg_id:
        seg = db.get(AudioSegment, seg_id)
    if seg is None and job.shot_id:
        track_map = {
            "generate_audio": ("dialogue", "narration"),
            "generate_bgm": ("bgm",),
            "generate_sfx": ("sfx", "ambient"),
        }
        tracks = track_map.get(job.job_type, ())
        if tracks:
            from sqlalchemy import select as _select
            candidates = db.execute(
                _select(AudioSegment).where(
                    AudioSegment.shot_id == job.shot_id,
                    AudioSegment.track_type.in_(tracks),
                    AudioSegment.generation_status.in_(("pending", "queued", "processing", "failed")),
                ).order_by(AudioSegment.created_at)
            ).scalars().all()
            if len(candidates) == 1:
                seg = candidates[0]
            elif len(candidates) > 1:
                # 无 audio_segment_id 且候选不唯一：不猜测绑定，避免音频挂错片段
                _write_event(db, job, "audio.segment_sync_ambiguous", {
                    "shot_id": job.shot_id,
                    "candidate_segment_ids": [c.id for c in candidates],
                    "note": "任务未携带 audio_segment_id，存在多个待生成片段，需人工指定",
                })
                return
    if seg is None:
        return
    seg.audio_asset_id = audio_asset["asset_id"]
    seg.generation_status = "completed"
    seg.review_status = "pending"
    # A4 依赖指纹：生成成功即固化「这条音频是用什么台词/音色/TTS 版本/参数产出的」。
    # 台词或音色之后再改，指纹就会不匹配，H3 复用判定据此拒绝旧音频（v2 §13.5）。
    try:
        from app.services.workflow import tts_fingerprint as _fp
        _wf = db.get(Workflow, job.workflow_id) if job.workflow_id else None
        _params = dict(job.params or {})
        _voice = _params.get("tts_voice")
        if not _voice and seg.voice_profile_id:
            from app.models.audio import VoiceProfile as _VP
            _vp = db.get(_VP, seg.voice_profile_id)
            _voice = getattr(_vp, "voice_id", None)
        seg.tts_fingerprint = _fp.compute(
            text=seg.text,
            voice_id=_voice,
            tts_workflow_id=job.workflow_id,
            tts_workflow_version=_wf.version if _wf is not None else None,
            tts_params=_params,
        )
    except Exception as _fp_exc:  # noqa: BLE001
        # 指纹计算失败不阻断音频落库，但要留痕：没有指纹的片段后续不会被复用
        _write_event(db, job, "audio.fingerprint_failed", {"error": str(_fp_exc)[:200]})
    try:
        import json as _json
        import subprocess as _sp
        proc = _sp.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", audio_asset["file_path"]],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            dur = float((_json.loads(proc.stdout).get("format") or {}).get("duration") or 0)
            if dur > 0:
                seg.duration_ms = int(round(dur * 1000))
    except Exception:  # noqa: BLE001
        pass
    _write_event(db, job, "audio.segment_synced", {
        "segment_id": seg.id,
        "asset_id": audio_asset["asset_id"],
        "duration_ms": seg.duration_ms,
    })


def _persist_shot_last_frame(db: Session, job: Job, created_assets: list[dict]) -> None:
    """clip 成功后提取实际末帧并绑定到 shot.last_frame_asset_id（P0 整改）。

    整改前 shot.last_frame_asset_id 只有读取、没有任何写入点，
    导致后续镜头永远拿不到上一镜尾帧，镜间连续性实际未生效。
    尾帧必须来自本次生成视频实际解码的最后一帧（extract_last_frame 含黑屏回退），
    不使用分镜图或预估帧；提取失败只记录事件，不阻断任务成功，
    但返回 False，调用方据此阻止链式下一环释放（避免下一镜缺首帧参数）。
    """
    if job.job_type != "generate_clip" or not job.shot_id:
        return False
    # 正常成功路径与对账续跑路径的 created_assets 只带 asset_id/out_name/file_path，
    # 不带 asset_type；仅按 asset_type 判断会永远取不到 clip，导致尾帧从未落库。
    # 这里同时兼容 out_name，保证两条成功路径都能绑定尾帧。
    clip = next(
        (
            a for a in created_assets
            if a.get("asset_type") == "clip"
            or (not a.get("asset_type") and a.get("out_name") == "clip")
        ),
        None,
    )
    if clip is None:
        return False
    shot = db.get(Shot, job.shot_id)
    if shot is None:
        return False
    clip_asset = db.get(Asset, clip["asset_id"])
    if clip_asset is None:
        return False
    try:
        from app.services.shot_chain import store_shot_last_frame
        result = store_shot_last_frame(db, shot, clip_asset, get_settings().media_dir)
    except Exception as e:  # noqa: BLE001
        _write_event(db, job, "shot.last_frame_failed", {
            "shot_id": shot.id,
            "error": f"{type(e).__name__}: {e}"[:300],
        })
        return False
    if result.get("ok"):
        _write_event(db, job, "shot.last_frame", {
            "shot_id": shot.id,
            "asset_id": result["asset_id"],
            "frame_timestamp": result.get("timestamp"),
            "frame_hash": result.get("frame_hash"),
            "fallback_used": result.get("fallback_used"),
        })
    else:
        _write_event(db, job, "shot.last_frame_failed", {
            "shot_id": shot.id,
            "error": result.get("error"),
        })
    return bool(result.get("ok"))


def _remove_file_if_unreferenced(db: Session, path: str | None) -> None:
    """删除不再被任何资产行引用的旧产物文件。

    跨项目同名保护：storyboard_N_img 历史上是平铺命名，多个项目的资产行可能指向
    同一路径（实测 3 个项目共用 storyboard_1_img.png）。原地覆盖时直接 os.remove
    会把别的项目仍在用的图删掉，因此先确认引用数为 0 再删。
    """
    import os

    if not path or not os.path.exists(path):
        return
    try:
        refs = db.query(Asset).filter(Asset.file_path == path).count()
    except Exception:  # noqa: BLE001
        return  # 查询失败时保守保留文件
    if refs > 0:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _store_shot_keyframe(db: Session, job: Job, shot: Shot, content: bytes, ext: str) -> dict:
    """分镜图每镜唯一：命名 storyboard_{项目内全局镜号}_img，重新生成原地覆盖。

    与尾帧一致的单资产模型：复用同一 Asset 行（同 id / 同名 / 同文件路径），
    不再产生历史版本；同镜旧分镜图资产连文件一起清理，保证资产库中
    每个镜头只有一张 storyboard_N_img，工作台可直接选中。
    """
    import os

    from app.models import AssetRef, JobInputRef, ids as id_gen
    from app.services.shot_chain import shot_global_index
    from app.services.thumbnail import make_thumb

    name = f"storyboard_{shot_global_index(db, shot)}_img"
    # 按项目分目录（2026-11-25 修复跨项目覆盖）：镜号是「项目内」全局序号，
    # 平铺在 storyboards/ 下会让两个项目都写 storyboard_1_img.png，
    # 后生成的项目直接覆盖前一个项目的图（实测 游乐场 / 末班地铁 / 本次测试项目
    # 三行资产同时指向 data/media/storyboards/storyboard_1_img.png）。
    pic_dir = os.path.join(str(get_settings().media_dir), "storyboards", str(job.project_id))
    os.makedirs(pic_dir, exist_ok=True)
    pic_path = os.path.join(pic_dir, f"{name}.{ext}")

    # 复用本镜当前生效分镜图，其次同名资产，都没有才新建
    asset = (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot.id,
            Asset.asset_type == "keyframe",
            Asset.validity == "current",
        )
        .order_by(Asset.version.desc())
        .first()
    )
    if asset is None:
        asset = (
            db.query(Asset)
            .filter(
                Asset.project_id == job.project_id,
                Asset.asset_type == "keyframe",
                Asset.name == name,
            )
            .first()
        )
    if asset is None:
        asset = Asset(
            id=id_gen.asset_id(),
            project_id=job.project_id,
            asset_type="keyframe",
            name=name,
            version=1,
            validity="current",
        )
        db.add(asset)
        db.flush()

    with open(pic_path, "wb") as f:
        f.write(content)

    old_path = asset.file_path
    asset.name = name
    asset.file_path = pic_path
    asset.source_job_id = job.id
    asset.validity = "current"
    asset.version = 1
    asset.quality_status = None
    asset.updated_at = _now().replace(tzinfo=None)
    asset.input_snapshot = job.input_snapshot
    db.flush()
    try:
        asset.thumb_path = make_thumb(pic_path, name)
    except Exception:  # noqa: BLE001
        asset.thumb_path = None
    # 旧文件仅在无其他资产行引用时删除（跨项目同名保护）
    if old_path and old_path != pic_path:
        _remove_file_if_unreferenced(db, old_path)
    db.flush()

    # 清理同镜历史分镜图，只保留当前这一张
    duplicates = (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot.id,
            Asset.asset_type == "keyframe",
            Asset.id != asset.id,
        )
        .all()
    )
    for dup in duplicates:
        db.query(AssetRef).filter(AssetRef.asset_id == dup.id).delete(synchronize_session=False)
        db.query(JobInputRef).filter(JobInputRef.asset_id == dup.id).delete(synchronize_session=False)
        dup_path = dup.file_path
        db.delete(dup)
        if dup_path and dup_path != pic_path and os.path.exists(dup_path):
            try:
                os.remove(dup_path)
            except OSError:
                pass
    db.flush()

    return {
        "asset_id": asset.id,
        "asset_type": "keyframe",
        "out_name": "cover",
        "file_path": pic_path,
    }


def _purge_asset_refs(db: Session, asset_id: str) -> None:
    """删除资产前清理全部外键引用，避免约束冲突导致覆盖落库失败。

    AssetRef / JobInputRef 为分镜与任务引用；SeriesAsset / EpisodeAssetRef
    为系列化创作的共享与每集引用，四张表均以 asset.id 为外键，必须先删。
    """
    from app.models import AssetRef, JobInputRef
    from app.models.series_asset import EpisodeAssetRef, SeriesAsset

    db.query(AssetRef).filter(AssetRef.asset_id == asset_id).delete(synchronize_session=False)
    db.query(JobInputRef).filter(JobInputRef.asset_id == asset_id).delete(
        synchronize_session=False
    )
    db.query(SeriesAsset).filter(SeriesAsset.asset_id == asset_id).delete(
        synchronize_session=False
    )
    db.query(EpisodeAssetRef).filter(EpisodeAssetRef.asset_id == asset_id).delete(
        synchronize_session=False
    )


def _store_shot_clip(db: Session, job: Job, shot: Shot, content: bytes, ext: str) -> dict:
    """分镜视频每镜唯一：命名 storyboard_{镜号}_clip，重新生成原地覆盖。

    与分镜图一致的单资产模型：复用同一 Asset 行（同 id / 同文件路径），
    不再产生历史版本；同镜旧视频资产连文件一起清理，保证资产库与分镜工作台
    每个镜头只保留一份当前视频，页面展示的就是重新生成后的那一份。
    """
    import os

    from app.models import AssetRef, JobInputRef, ids as id_gen
    from app.services.shot_chain import shot_global_index

    name = f"storyboard_{shot_global_index(db, shot)}_clip"
    # 按项目分目录，理由同 _store_shot_keyframe（镜号是项目内序号，平铺会跨项目覆盖）
    clip_dir = os.path.join(str(get_settings().media_dir), "storyboards", str(job.project_id))
    os.makedirs(clip_dir, exist_ok=True)
    clip_path = os.path.join(clip_dir, f"{name}.{ext}")

    # 复用本镜当前生效视频，其次同名资产，都没有才新建
    asset = (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot.id,
            Asset.asset_type == "clip",
            Asset.validity == "current",
        )
        .order_by(Asset.version.desc())
        .first()
    )
    if asset is None:
        asset = (
            db.query(Asset)
            .filter(
                Asset.project_id == job.project_id,
                Asset.asset_type == "clip",
                Asset.name == name,
            )
            .first()
        )
    if asset is None:
        asset = Asset(
            id=id_gen.asset_id(),
            project_id=job.project_id,
            asset_type="clip",
            name=name,
            version=1,
            validity="current",
        )
        db.add(asset)
        db.flush()

    with open(clip_path, "wb") as f:
        f.write(content)

    old_path = asset.file_path
    asset.name = name
    asset.file_path = clip_path
    asset.source_job_id = job.id
    asset.validity = "current"
    asset.version = 1
    asset.quality_status = None
    # 破前端缓存：前端以 v=updated_at 拉取文件，必须变化才能看到新视频
    asset.updated_at = _now().replace(tzinfo=None)
    asset.input_snapshot = job.input_snapshot
    db.flush()
    # 旧文件仅在无其他资产行引用时删除（跨项目同名保护）
    if old_path and old_path != clip_path:
        _remove_file_if_unreferenced(db, old_path)
    db.flush()

    # 清理同镜历史分镜视频，只保留当前这一份
    duplicates = (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot.id,
            Asset.asset_type == "clip",
            Asset.id != asset.id,
        )
        .all()
    )
    for dup in duplicates:
        _purge_asset_refs(db, dup.id)
        dup_path = dup.file_path
        db.delete(dup)
        if dup_path and dup_path != clip_path and os.path.exists(dup_path):
            try:
                os.remove(dup_path)
            except OSError:
                pass
    db.flush()

    return {
        "asset_id": asset.id,
        "asset_type": "clip",
        "out_name": "clip",
        "file_path": clip_path,
    }


def _requeue_for_quality_retry(db: Session, job: Job, failures: list[dict]) -> None:
    """图片质检不合格 → 旧产物转 superseded，任务重新排队换 seed 重采。

    必须同时清掉 prompt_id / submission_state，否则下次执行会命中执行器第 0 步的
    「已提交远端 → 续跑下载」分支：把同一份不合格产物重新下载一遍，既没换 seed
    也不消耗新算力，attempts 增加但产物字节完全相同。
    实测（2026-11-25 job_95860fc75897 场景基准图）：attempt 1/2 产物 md5 均为
    5063f8fa…，第二次事件是 job.reconciled 而非 job.submitted。

    params 是普通 JSON 列，原地 pop 不会被 SQLAlchemy 标脏，必须整体重新赋值，
    否则残留 seed 会让新一次提交生成完全相同的图。
    """
    for _qf in failures:
        _bad = db.get(Asset, _qf["asset_id"])
        if _bad is not None:
            _bad.validity = "superseded"
    job.prompt_id = None
    # submission_state 是 NOT NULL 列（默认 not_submitted），不能用 None
    job.submission_state = "not_submitted"
    job.status = "queued"
    if job.params:
        job.params = {k: v for k, v in job.params.items() if k != "seed"}
    _write_event(db, job, "job.quality_retry", {
        "attempt": job.attempts, "failed": failures,
    })
    db.commit()


def _store_entity_asset(
    db: Session, job: Job, asset_type: str, name: str, content: bytes, ext: str
) -> dict | None:
    """实体资产（角色定妆图 / 场景基准图 / 道具锚定图）重新生成：原地覆盖。

    已有同名 current 资产时沿用其 id / 文件路径原地覆盖，并把同范围内的历史
    资产连文件一起清理，保证资产库每个实体只保留一份当前图；返回 None 表示
    该实体尚无资产，调用方按首次生成流程处理。
    """
    import os

    existing = (
        db.query(Asset)
        .filter(
            Asset.project_id == job.project_id,
            Asset.asset_type == asset_type,
            Asset.name == name,
            Asset.validity == "current",
        )
        .order_by(Asset.version.desc())
        .first()
    )
    if existing is None:
        # 质量重试会把不合格产物先置 superseded 再重新排队，此时没有任何 current 行；
        # 若这里直接返回 None，调用方会新建一行，重试几次就多几行同内容资产
        # （实测 2026-11-25 场景「便利店」出现 ast_6ca8…v1 / ast_03cd…v2 两条，
        # md5 完全相同）。因此退化为「同名最新一行原地复用」，保证每实体只有一份。
        existing = (
            db.query(Asset)
            .filter(
                Asset.project_id == job.project_id,
                Asset.asset_type == asset_type,
                Asset.name == name,
            )
            .order_by(Asset.version.desc())
            .first()
        )
    if existing is None:
        return None
    existing.validity = "current"

    filepath = _overwrite_asset_file(existing, content)
    try:
        from app.services.thumbnail import make_thumb
        existing.thumb_path = make_thumb(
            filepath, os.path.splitext(os.path.basename(filepath))[0]
        )
    except Exception:
        pass
    _snap = dict(existing.input_snapshot or {})
    _snap["regenerated_at"] = _now().isoformat()
    _snap["regenerate_job_id"] = job.id
    existing.input_snapshot = _snap
    existing.source_job_id = job.id

    duplicates = (
        db.query(Asset)
        .filter(
            Asset.project_id == job.project_id,
            Asset.asset_type == asset_type,
            Asset.name == name,
            Asset.id != existing.id,
        )
        .all()
    )
    for dup in duplicates:
        # 实体主图指针若指向将被清理的旧版本，先切到保留版本，避免悬空引用
        if asset_type == "character":
            for ch in db.query(Character).filter(Character.portrait_asset_id == dup.id).all():
                ch.portrait_asset_id = existing.id
        elif asset_type == "scene":
            for sc in db.query(Scene).filter(Scene.baseline_asset_id == dup.id).all():
                sc.baseline_asset_id = existing.id
        elif asset_type == "prop":
            for pr in db.query(Prop).filter(Prop.anchor_asset_id == dup.id).all():
                pr.anchor_asset_id = existing.id
        # 先清理引用记录，否则外键约束会阻止删除历史资产
        _purge_asset_refs(db, dup.id)
        dup_path = dup.file_path
        db.delete(dup)
        if dup_path and dup_path != filepath and os.path.exists(dup_path):
            try:
                os.remove(dup_path)
            except OSError:
                pass
    db.flush()

    return {
        "asset_id": existing.id,
        "asset_type": existing.asset_type,
        "out_name": "cover",
        "file_path": filepath,
    }


def _release_chained_next(db: Session, job: Job) -> None:
    """链式生成（chained）：本任务成功后释放链上下一个 chained_wait 任务。

    链式关系记录在下一任务的 input_snapshot.chain.prev_job_id；
    仅当前一任务（本任务）成功时释放，失败时链条保持暂停，
    可重新发起批量（在途过滤含 chained_wait，不会重复提交）。
    """
    if job.job_type != "generate_clip":
        return
    next_jobs = (
        db.query(Job)
        .filter(
            Job.status == "chained_wait",
            Job.job_type == "generate_clip",
            Job.project_id == job.project_id,
        )
        .all()
    )
    for nj in next_jobs:
        chain = (nj.input_snapshot or {}).get("chain") or {}
        if chain.get("prev_job_id") != job.id:
            continue
        nj.status = "queued"
        _write_event(db, nj, "chain.released", {
            "prev_job_id": job.id,
            "note": "上一镜已成功，其尾帧已绑定为本镜首帧引用，本镜开始排队生成",
        })
        _write_event(db, job, "chain.next_released", {
            "next_job_id": nj.id,
            "note": "已提取本镜尾帧作为下一镜头首帧，下一镜开始生成；串行链式执行中，请勿中断或重新发起批量",
        })


def _cancel_chained_after_failure(db: Session, job: Job, code: str) -> None:
    """链式生成（chained）：前序任务失败时级联取消链上等待任务。

    整改前只在成功时释放下一环，前序失败后链条永久暂停：后续任务停在
    chained_wait，既不会自动推进，又会被 _filter_inflight 视为在途而
    无法重新提交，前端刷新后表现为『待生成视频』的假回退。
    这里沿 input_snapshot.chain.prev_job_id 逐环向下置 cancelled 并写事件，
    取消后不再算在途，可直接重新发起批量提交。
    """
    if job.job_type != "generate_clip":
        return
    frontier = [job.id]
    cancelled_total = 0
    while frontier:
        waiting = (
            db.query(Job)
            .filter(
                Job.status == "chained_wait",
                Job.job_type == "generate_clip",
                Job.project_id == job.project_id,
            )
            .all()
        )
        nxt: list[str] = []
        for nj in waiting:
            chain = (nj.input_snapshot or {}).get("chain") or {}
            prev_id = chain.get("prev_job_id")
            if prev_id not in frontier:
                continue
            nj.status = "cancelled"
            nj.error_code = "chain_prev_failed"
            nj.error_msg = f"前序镜头任务失败（{code}），链式后续任务已取消"[:2000]
            nj.finished_at = _now()
            _write_event(db, nj, "chain.cancelled", {
                "prev_job_id": prev_id,
                "reason": "prev_failed",
                "prev_error_code": code,
            })
            nxt.append(nj.id)
            cancelled_total += 1
        frontier = nxt
    if cancelled_total:
        print(f"[worker] 链式级联取消 {cancelled_total} 个后续任务（前序 {job.id} 失败）", flush=True)



def _preferred_instance_for_shot(db: Session, job: Job, exclude_instances: list[str]) -> dict | None:
    """同镜头亲和性（D-22）：查询同镜头最近成功任务使用的实例，优先复用。

    依据：同镜头的 keyframe/clip/audio 存在输入依赖（clip 用 keyframe 做首帧），
    复用同实例可复用已上传的媒体文件，减少跨实例上传与下载。
    返回实例配置 dict（{base_url, weight, enabled}）或 None（无亲和记录）。"""
    if not job.shot_id:
        return None
    try:
        prev = (
            db.query(Job)
            .filter(
                Job.shot_id == job.shot_id,
                Job.status == "succeeded",
                Job.id != job.id,
            )
            .order_by(Job.finished_at.desc())
            .first()
        )
    except Exception:
        return None
    if prev is None:
        return None
    snapshot = prev.input_snapshot or {}
    base_url = snapshot.get("comfyui_instance")
    if not base_url or base_url in exclude_instances:
        return None
    # 仅当该实例仍在启用列表中才复用
    for inst in comfyui_pool.enabled_instances():
        if inst["base_url"] == base_url:
            return inst
    return None


def execute_generate_job(job_id: str) -> dict:
    """执行一个生成类任务（任务级故障转移重试入口，D-21）。

    提交前阶段失败（实例不可达/上传失败/提交失败）在未尝试过的实例间自动换实例
    重试；等待阶段实例失联（被删除/连接中断，连续 3 次连不上）同样换实例重试；
    其余提交后阶段的错误（超时/执行错误）不换实例，保持原失败语义。
    返回执行结果摘要（供 worker 记录）。"""
    exclude_instances: list[str] = []
    result = _execute_generate_job_once(job_id, exclude_instances)
    while (not result.get("ok")) and result.get("retriable"):
        exclude_instances = result.get("exclude_instances", exclude_instances)
        result = _execute_generate_job_once(job_id, exclude_instances)
    return result


def _execute_generate_job_once(job_id: str, exclude_instances: list[str]) -> dict:
    """单次执行一个生成类任务。exclude_instances 为本次尝试需排除的实例 base_url。"""
    settings = get_settings()
    # 运行时查找 SessionLocal：跟随 app.core.database 的当前绑定（测试替换引擎时保持一致）
    db: Session = _db.SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        # running 状态允许进入：任务级故障转移重试场景（首次为 queued/submission_unknown）
        if job.status not in ("queued", "submission_unknown", "running"):
            return {"ok": False, "reason": f"invalid_status:{job.status}"}

        if job.status != "running":
            # 认领必须原子：调度器按并发度（ComfyUI 实例数）多线程取任务，
            # 同一 queued 任务可能被连续取到两次（工作线程尚未置 running 的窗口期），
            # 无条件赋值会让同一任务提交两次、产出两个资产版本。此处用条件 UPDATE 抢占。
            now = _now()
            claimed = (
                db.query(Job)
                .filter(Job.id == job_id, Job.status.in_(("queued", "submission_unknown")))
                .update(
                    {
                        "status": "running",
                        "started_at": now,
                        "attempts": func.coalesce(Job.attempts, 0) + 1,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            db.refresh(job)
            if not claimed:
                # 已被其他执行线程认领，直接退出，避免重复提交
                return {"ok": False, "reason": f"already_claimed:{job.status}"}
            _write_event(db, job, "job.started", {"attempt": job.attempts})
            # 镜头状态流转：进入生成中（§3.2）
            if job.shot_id:
                _shot = db.get(Shot, job.shot_id)
                if _shot is not None and _shot.status != "generating":
                    _shot.status = "generating"
            db.commit()

        # 统一资产就绪门禁二次校验：任务创建与真正执行之间存在时间差，
        # 执行前必须重新校验资产就绪状态，防止入队后资产被重新生成或失效
        # 仍提交 ComfyUI。实体资产图（定妆/基准/锚定图）自身不适用该门禁。
        if job.job_type == "generate_keyframe":
            _params_peek = dict(job.params or {})
            _is_entity_asset = any(
                k in _params_peek for k in ("__character_id__", "__scene_id__", "__prop_id__")
            )
            if not _is_entity_asset:
                from app.services.assets_gate import evaluate_assets_gate

                _gate = evaluate_assets_gate(db, job.project_id)
                if not _gate.ready:
                    _gate_detail = {
                        "gate_status": _gate.status,
                        "reason_code": _gate.reason_code,
                        "blockers": [b.to_dict() for b in _gate.blockers],
                    }
                    _fail_job(
                        db,
                        job,
                        _gate.reason_code,
                        f"资产就绪门禁未通过：{_gate.message}",
                    )
                    # 门禁明细持久化到 input_snapshot 供追溯与前端展示
                    _snap = dict(job.input_snapshot or {})
                    _snap["assets_gate"] = _gate_detail
                    job.input_snapshot = _snap
                    db.commit()
                    return {"ok": False, "reason": f"assets_gate:{_gate.reason_code}"}

        wf_record = db.get(Workflow, job.workflow_id) if job.workflow_id else None
        if wf_record is None:
            _fail_job(db, job, "no_matching_workflow", "任务未关联工作流")
            return {"ok": False, "reason": "no_workflow"}

        params = dict(job.params or {})
        media_files = params.pop("__media__", {})  # {param_name: local_file_path}
        # P4 整改：资产图提示词模板版本从提交参数剥离，持久化到 input_snapshot 供追溯
        _prompt_version = params.pop("__asset_prompt_version__", None)
        if _prompt_version:
            _pv_snap = dict(job.input_snapshot or {})
            _pv_snap["asset_prompt_version"] = _prompt_version
            _pv_params = _pv_snap.get("params")
            if isinstance(_pv_params, dict):
                _pv_snap["params"] = {k: v for k, v in _pv_params.items() if k != "__asset_prompt_version__"}
            job.input_snapshot = _pv_snap
            db.commit()
        # 实体锚定图类型（D-21）：定妆图/基准图/锚定图按实体归属落 character/scene/prop，
        # 与 enums.ASSET_TYPE（权威枚举，DATABASE.md §asset）及前端资产库分类保持一致；
        # 分镜产物仍为 keyframe/clip/audio，不受影响。
        # 同时取实体名称作为资产名：资产库按名称搜索（Asset.name contains）才能命中角色/场景/道具名
        _entity_asset_type = None
        _entity_asset_name = None
        _ceid = params.get("__character_id__")
        _seid = params.get("__scene_id__")
        _peid = params.get("__prop_id__")
        if _ceid:
            _entity_asset_type = "character"
            _entity_obj = db.get(Character, _ceid)
            _entity_asset_name = getattr(_entity_obj, "name", None) if _entity_obj else None
        elif _seid:
            _entity_asset_type = "scene"
            _entity_obj = db.get(Scene, _seid)
            if _entity_obj is not None:
                _entity_asset_name = _entity_obj.title or _seid
        elif _peid:
            _entity_asset_type = "prop"
            _entity_obj = db.get(Prop, _peid)
            _entity_asset_name = getattr(_entity_obj, "name", None) if _entity_obj else None
        # 图片任务画布注入：分镜图/场景基准图按项目画幅出图，角色/道具按固定画幅。
        # 重生成链路已在建任务时剔除历史画布参数，这里只要齐全就注入；
        # 显式同时提供 width/height 时尊重调用方（WORKFLOW.md §5.2）。
        if job.job_type == "generate_keyframe" and not (
            isinstance(params.get("width"), int) and isinstance(params.get("height"), int)
        ):
            _project_img = db.get(Project, job.project_id)
            _canvas_type = _entity_asset_type
            if _canvas_type is None and not job.shot_id:
                # 非分镜的资产图（如角色+道具结合图）保持 9:16，不跟随项目画幅
                _canvas_type = "asset"
            params["width"], params["height"] = _image_canvas_size(
                getattr(_project_img, "aspect_ratio", None), _canvas_type
            )
            _snap_img = dict(job.input_snapshot or {})
            _snap_img["injected_params"] = {
                k: params[k] for k in ("width", "height") if k in params
            }
            job.input_snapshot = _snap_img
            db.commit()
        seed = params.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)  # random 策略：本 job 内所有种子共用
        params["seed"] = seed
        # 持久化 seed 到 job.params，重试时复用相同 seed（D-21 修复）
        if job.params is None:
            job.params = {}
        if job.params.get("seed") is None:
            job.params["seed"] = seed
            db.commit()
        # clip 任务自动参数注入（WORKFLOW.md §5.2/§8/§13.1）：
        # - 当前镜头分镜图 → ref_image（未显式提供时）
        # - 上一镜头末帧 → ref_prev_frame（存在时）
        # - 按 project.aspect_ratio 注入 H3 width/height（§5.2 分辨率表）
        # - 按 shot.duration 对齐帧数 length（fps=24，步长 17，offset 5，范围 124-362）
        if job.job_type == "generate_clip" and job.shot_id:
            _shot = db.get(Shot, job.shot_id)
            if _shot is not None:
                if "ref_image" not in media_files:
                    _kf = (
                        db.query(Asset)
                        .join(Job, Job.id == Asset.source_job_id)
                        .filter(Job.shot_id == _shot.id, Asset.validity == "current",
                                Asset.asset_type == "keyframe", Job.status == "succeeded")
                        .order_by(Asset.version.desc())
                        .first()
                    )
                    if _kf is not None:
                        media_files["ref_image"] = _kf.file_path
                # 批量生成方式（弹窗选择）：independent 不注入上一镜尾帧；
                # chained 在执行期解析上一镜实际尾帧（前一任务成功后已绑定 last_frame）
                _chain_mode = (job.input_snapshot or {}).get("chain_mode")
                if _chain_mode == "independent":
                    media_files.pop("ref_prev_frame", None)
                elif _chain_mode == "chained":
                    if "ref_prev_frame" not in media_files:
                        from app.services.shot_chain import resolve_prev_frame
                        _pf = resolve_prev_frame(db, _shot)
                        if _pf is not None:
                            media_files["ref_prev_frame"] = _pf["file_path"]
                    # 尾帧一致性门禁：与 api/v1/generate.py 的 409 prev_frame_missing 同口径。
                    # chained 模式声明承接上一镜尾帧却解析不到时，必须明确失败并留痕，
                    # 不再静默跳过——静默跳过会让尾帧约束在成片里悄悄消失，
                    # 相邻镜头人物位置与朝向对不上（Part B 核查结论）。
                    if "ref_prev_frame" not in media_files:
                        _need_prev = bool(getattr(_shot, "requires_prev_frame", False))
                        _write_event(db, job, "job.prev_frame_missing", {
                            "episode": _shot.episode,
                            "seq": _shot.seq,
                            "requires_prev_frame": _need_prev,
                            "chain_mode": _chain_mode,
                            "note": (
                                "chained 模式未解析到上一镜尾帧"
                                + ("，按镜头要求阻断本次出片" if _need_prev
                                   else "，已降级为无尾帧约束（开场状态按剧情与分镜图推断）")
                            ),
                        })
                        _snap_pf = dict(job.input_snapshot or {})
                        _snap_pf["prev_frame_missing"] = {
                            "episode": _shot.episode,
                            "seq": _shot.seq,
                            "requires_prev_frame": _need_prev,
                        }
                        job.input_snapshot = _snap_pf
                        if _need_prev:
                            job.status = "failed"
                            job.error_code = "prev_frame_missing"
                            job.error_msg = (
                                f"第{_shot.episode}集第{_shot.seq}镜要求承接上一镜尾帧，"
                                "但未能解析到上一镜末帧（上一镜未出片或尾帧已失效），"
                                "请先生成上一镜视频后重试"
                            )[:500]
                            job.finished_at = _now()
                        db.commit()
                        if _need_prev:
                            return
                elif "ref_prev_frame" not in media_files and _shot.last_frame_asset_id:
                    _lf = db.get(Asset, _shot.last_frame_asset_id)
                    if _lf is not None:
                        media_files["ref_prev_frame"] = _lf.file_path
                # 跨集连续性：非第 1 集的首镜 clip，注入上一集末帧作为 first_frame
                # （连续性校验通过时才注入；校验不通过则无首帧约束，由连续性流程处理）
                if "first_frame" not in media_files and _shot.episode > 1:
                    try:
                        _is_first_shot = (
                            db.query(Shot)
                            .join(Scene, Shot.scene_id == Scene.id)
                            .filter(Scene.project_id == job.project_id,
                                    Shot.episode == _shot.episode)
                            .order_by(Scene.seq, Shot.seq)
                            .first()
                        )
                        if _is_first_shot is not None and _is_first_shot.id == _shot.id:
                            from app.services.continuity import get_first_frame_for_episode
                            _ff = get_first_frame_for_episode(db, job.project_id, _shot.episode)
                            if _ff:
                                media_files["first_frame"] = _ff
                    except Exception:
                        pass  # 首帧注入失败不阻断生成，回退为无首帧约束
                # H3 参考音频（音色/口型引导）：按说话人各注入一条 ref_audio_N，
                # 同一角色跨镜头复用同一音色，双角色镜头不再把两个人声音混成一条
                _audio_inputs = _shot_ref_audios(db, _shot)
                _ref_audio_ms = _audio_inputs["total_ms"]
                # A4 指纹失配留痕：台词/音色/TTS 版本变了 → 旧音频不注入，明确告诉用户要重跑 TTS
                if _audio_inputs["stale"]:
                    _write_event(db, job, "job.audio_tts_stale", {
                        "stale": _audio_inputs["stale"],
                        "note": "以下片段配音依赖已变更，未注入 H3 参考音频，需重新生成 TTS 后再出视频",
                    })
                    _snap_st = dict(job.input_snapshot or {})
                    _snap_st["audio_tts_stale"] = _audio_inputs["stale"]
                    job.input_snapshot = _snap_st
                    db.commit()
                # A4 宽限期告警：历史片段无指纹，本次按审核状态复用（不阻断存量项目，但留痕）
                if _audio_inputs.get("legacy"):
                    _write_event(db, job, "audio.fingerprint_missing", {
                        "segments": _audio_inputs["legacy"],
                        "note": "历史片段无依赖指纹，宽限期按审核状态复用；重新生成后自动补齐",
                    })
                    _snap_lg = dict(job.input_snapshot or {})
                    _snap_lg["audio_fingerprint_missing"] = _audio_inputs["legacy"]
                    job.input_snapshot = _snap_lg
                    db.commit()
                # A3 音频输入校验（v2 §7.6/§8.4）：注入前用 ffprobe 核对真实文件，
                # 不合格就明确失败并指出是哪个槽位/哪个角色，不让坏音频传到 ComfyUI
                if _audio_inputs["bindings"]:
                    from app.services.workflow import audio_check
                    _audio_cons = audio_check.constraints_from_capabilities(
                        (wf_record.capabilities or {})
                    )
                    _checks = []
                    for _binding, _path in zip(_audio_inputs["bindings"], _audio_inputs["paths"]):
                        _checks.append({**_binding, "file_path": _path})
                    _verdict = audio_check.check_audio_batch(_checks, _audio_cons)
                    _snap_ac = dict(job.input_snapshot or {})
                    _snap_ac["audio_input_checks"] = _verdict
                    job.input_snapshot = _snap_ac
                    db.commit()
                    if not _verdict["ok"]:
                        _msgs = [
                            v for c in _verdict["checks"] if not c["ok"] for v in c["violations"]
                        ]
                        _write_event(db, job, "job.audio_input_invalid", {
                            "failed_slots": _verdict["failed_slots"],
                            "violations": _msgs,
                            "constraints": _audio_cons,
                            "note": "参考音频不满足工作流输入要求，任务未提交",
                        })
                        job.status = "failed"
                        job.error_code = "audio_input_invalid"
                        job.error_msg = "；".join(_msgs)[:500]
                        job.finished_at = _now()
                        db.commit()
                        return
                # 槽位→文件路径按 bindings 顺序回填（paths 与 bindings 同序）
                for _binding, _path in zip(_audio_inputs["bindings"], _audio_inputs["paths"]):
                    if _path and _binding["slot"] not in media_files:
                        media_files[_binding["slot"]] = _path
                if _audio_inputs["overflow"]:
                    # 槽位不足：显式告警并留痕，不静默丢掉第 4 个说话人的声音
                    _write_event(db, job, "job.audio_slot_overflow", {
                        "dropped": _audio_inputs["overflow"],
                        "max_audio_inputs": len(_audio_inputs["paths"]),
                        "note": "H3 参考音频槽位不足，以下说话人未注入音频",
                    })
                    _snap_ov = dict(job.input_snapshot or {})
                    _snap_ov["audio_slot_overflow"] = _audio_inputs["overflow"]
                    job.input_snapshot = _snap_ov
                    db.commit()
                _project = db.get(Project, job.project_id)
                if _project is not None and "width" not in params and "height" not in params:
                    # 档位工作流（极速 480×864 / 标准 640×1120 / 质量 768×1344）自带画幅优先，
                    # 否则维持原逻辑：按项目宽高比注入
                    _tier_wh = None
                    from app.services.workflow.capabilities import workflow_tier

                    if workflow_tier(wf_record.capabilities):
                        _tier_wh = _workflow_frame_size(wf_record.definition)
                    if _tier_wh is not None:
                        params["width"], params["height"] = _tier_wh
                    else:
                        _h3_wh = {"9:16": (736, 1312), "16:9": (1312, 736), "1:1": (768, 768)}
                        if _project.aspect_ratio in _h3_wh:
                            params["width"], params["height"] = _h3_wh[_project.aspect_ratio]
                if "length" not in params and _shot.duration:
                    if _ref_audio_ms:
                        # 配音优先：按配音总时长 + 0.3s 余量，对齐 24fps 与 17n+5 网格
                        import math
                        _frames = int(math.ceil((_ref_audio_ms / 1000.0 + 0.3) * 24))
                        _n = max(7, min(21, int(math.ceil((_frames - 5) / 17.0))))
                    else:
                        _target = float(_shot.duration) * 24
                        _n = max(7, min(21, round((_target - 5) / 17)))
                    params["length"] = 17 * _n + 5
                # 持久化注入参数到 input_snapshot（可追溯：实际提交的生成参数与参考图）
                _snap = dict(job.input_snapshot or {})
                _snap["injected_params"] = {
                    k: params[k] for k in ("width", "height", "length") if k in params
                }
                _snap["injected_media"] = dict(media_files)
                # 角色↔音频槽位绑定：任务详情可回答「ref_audio_2 是谁的声音」
                if _audio_inputs["bindings"]:
                    _snap["audio_bindings"] = _audio_inputs["bindings"]
                job.input_snapshot = _snap
                db.commit()

        # 0. submission_unknown 续跑分支（D-23 修复）：已有 prompt_id 时跳过上传/提交，
        # 直接查远端 history 续跑下载，避免重新提交浪费算力。
        # comfy_timeout 对账续跑（scheduler._reconcile_comfy_timeout_jobs）：任务重置为 queued
        # 并打 resume_prompt 标记，进入执行器后状态已翻转为 running，因此同时识别该标记。
        _resume_via_prompt = bool((job.input_snapshot or {}).get("resume_prompt"))
        # submission_unknown 与 submitted 均为已提交远端的状态：前者表示提交后本地未保存确认，
        # 后者表示已确认提交。worker 重启时 _recover_interrupted_jobs 把 running 重置为 queued，
        # submission_state 保留 submitted，此时必须走续跑路径而非重新提交（否则同一个镜头重复入队远端）。
        if not settings.render_mock and job.prompt_id and (
            _resume_via_prompt or job.status == "submission_unknown" or job.submission_state == "submitted"
        ):
            snapshot = job.input_snapshot or {}
            inst_url = snapshot.get("comfyui_instance")
            if inst_url:
                try:
                    client = ComfyUIClient(base_url=inst_url)
                    used_instance = {"base_url": inst_url, "weight": 1, "enabled": True}
                    history = client.get_history(job.prompt_id)
                    if history is not None:
                        done, err = client.is_job_done(history)
                        if done and not err:
                            # 远端已完成：直接跳到产物提取下载（跳过上传/提交/等待）
                            _write_event(db, job, "job.reconciled", {
                                "prompt_id": job.prompt_id,
                                "instance": inst_url,
                            })
                            db.commit()
                            # 跳转到产物提取（第 5 步）
                            outputs = client.extract_outputs(history)
                            output_map = find_output_nodes(wf_record.definition)
                            downloaded = []
                            try:
                                for out_name, node_id in output_map.items():
                                    items = outputs.get(node_id, [])
                                    if not items:
                                        _fail_job(db, job, "output_missing", f"输出 {out_name}（节点 {node_id}）未产出文件")
                                        return {"ok": False, "reason": "output_missing"}
                                    for item in items:
                                        content = client.download_file(item["filename"], item.get("subfolder", ""), item.get("type", "output"))
                                        downloaded.append({"out_name": out_name, "item": item, "content": content})
                            except ComfyUIError as e:
                                _fail_job(db, job, e.code, e.message)
                                return {"ok": False, "reason": e.code}
                            # 跳转到资产落库（第 6 步）——使用 goto 模式：设置标志后跳过后续步骤
                            # 由于 Python 没有 goto，这里直接内联资产落库逻辑
                            from app.models import ids as id_gen
                            import os
                            media_dir = settings.media_dir
                            os.makedirs(media_dir, exist_ok=True)
                            created_assets = []
                            _regen_target = _asset_regenerate_target(db, job)
                            for d in downloaded:
                                out_name = d["out_name"]
                                item = d["item"]
                                content = d["content"]
                                ext = (item["filename"].rsplit(".", 1)[-1] or "png").lower()
                                asset_type_map = {"cover": "keyframe", "clip": "clip", "voice": "audio", "bgm": "audio", "sfx": "audio"}
                                asset_type = asset_type_map.get(out_name, "keyframe")
                                if asset_type == "keyframe" and _entity_asset_type:
                                    asset_type = _entity_asset_type
                                if _regen_target is not None:
                                    # 资产库 regenerate：原地覆盖原文件，名称与位置固定不变
                                    filepath = _overwrite_asset_file(_regen_target, content)
                                    try:
                                        from app.services.thumbnail import make_thumb
                                        _regen_target.thumb_path = make_thumb(
                                            filepath, os.path.splitext(os.path.basename(filepath))[0]
                                        )
                                    except Exception:
                                        pass
                                    _rt_snap = dict(_regen_target.input_snapshot or {})
                                    _rt_snap["regenerated_at"] = _now().isoformat()
                                    _rt_snap["regenerate_job_id"] = job.id
                                    _regen_target.input_snapshot = _rt_snap
                                    created_assets.append({
                                        "asset_id": _regen_target.id,
                                        "out_name": out_name,
                                        "file_path": filepath,
                                    })
                                    continue
                                if asset_type == "keyframe" and job.shot_id:
                                    _resume_shot = db.get(Shot, job.shot_id)
                                    if _resume_shot is not None:
                                        created_assets.append(
                                            _store_shot_keyframe(db, job, _resume_shot, content, ext)
                                        )
                                        continue
                                # 分镜视频与分镜图同口径：每镜只保留一份，重新生成原地覆盖
                                if asset_type == "clip" and job.shot_id:
                                    _resume_shot = db.get(Shot, job.shot_id)
                                    if _resume_shot is not None:
                                        created_assets.append(
                                            _store_shot_clip(db, job, _resume_shot, content, ext)
                                        )
                                        continue
                                # 资产图（角色定妆图/场景基准图/道具锚定图）重新生成：
                                # 已有同名 current 资产时原地覆盖并清掉历史，只保留一份
                                if _entity_asset_type and _entity_asset_name:
                                    _overwritten = _store_entity_asset(
                                        db, job, _entity_asset_type, _entity_asset_name, content, ext
                                    )
                                    if _overwritten is not None:
                                        created_assets.append(_overwritten)
                                        continue
                                asset_id = id_gen.asset_id()
                                # 版本号必须在产物类型与实体归属确定之后再计算，
                                # 否则会用到尚未赋值的类型变量，且实体类资产无法按名称隔离版本
                                version = _next_asset_version(db, job, asset_type, _entity_asset_name)
                                _supersede_previous_versions(db, job, asset_type, _entity_asset_name)
                                filename = f"{asset_id}_v{version}.{ext}"
                                filepath = os.path.join(media_dir, filename)
                                with open(filepath, "wb") as f_out:
                                    f_out.write(content)
                                asset = Asset(
                                    id=asset_id, project_id=job.project_id,
                                    asset_type=asset_type,
                                    name=(
                                        _entity_asset_name
                                        if (_entity_asset_name and _entity_asset_type)
                                        else f"{out_name}_{job.id[-6:]}"
                                    ),
                                    version=version, file_path=filepath,
                                    thumb_path=None,
                                    source_job_id=job.id,
                                    validity="current",
                                    input_snapshot=job.input_snapshot,
                                )
                                db.add(asset)
                                db.flush()
                                # 缩略图（D-17，失败不阻断）
                                try:
                                    from app.services.thumbnail import make_thumb
                                    thumb_path = make_thumb(filepath, asset_id)
                                    asset.thumb_path = thumb_path
                                except Exception:
                                    asset.thumb_path = None
                                created_assets.append({"asset_id": asset_id, "asset_type": asset_type, "out_name": out_name, "file_path": filepath})
                            # 锚定图回写（D-20）
                            _prop_id = (job.params or {}).get("__prop_id__")
                            _character_id = (job.params or {}).get("__character_id__")
                            _scene_id = (job.params or {}).get("__scene_id__")
                            if _prop_id:
                                _p = db.get(Prop, _prop_id)
                                # 质量重试会生成新的 current 版本并废弃旧版本，实体指针必须跟随最新产物
                                if _p:
                                    _kf = [a for a in created_assets if a["out_name"] == "cover"]
                                    if _kf:
                                        _p.anchor_asset_id = _kf[0]["asset_id"]
                            if _character_id:
                                _c = db.get(Character, _character_id)
                                if _c:
                                    _kf = [a for a in created_assets if a["out_name"] == "cover"]
                                    if _kf:
                                        _c.portrait_asset_id = _kf[0]["asset_id"]
                            if _scene_id:
                                _s = db.get(Scene, _scene_id)
                                if _s:
                                    _kf = [a for a in created_assets if a["out_name"] == "cover"]
                                    if _kf:
                                        _s.baseline_asset_id = _kf[0]["asset_id"]
                            # 图片质量检查回写（与主成功路径口径一致）：此前该对账续跑路径
                            # 漏掉质量检查，导致产物 quality_status 永远停留 pending，
                            # image_quality 验收与道具视觉校验均无法核验（2026-11-06 排查确认）
                            if not settings.render_mock:
                                _IMG_TYPES = ("keyframe", "character", "scene", "prop")
                                for _a in created_assets:
                                    if _a.get("asset_type") not in _IMG_TYPES:
                                        continue
                                    try:
                                        from app.services.image_quality import check_image_quality
                                        _qr = check_image_quality(
                                            _a["file_path"], asset_type=_a["asset_type"]
                                        )
                                    except Exception as _qe:  # noqa: BLE001
                                        _qr = {"passed": False,
                                               "issues": [f"质量检查异常: {type(_qe).__name__}"],
                                               "checks": {}}
                                    _qa = db.get(Asset, _a["asset_id"])
                                    if _qa is not None:
                                        _qa.quality_status = "passed" if _qr["passed"] else "failed"
                                        _qa.quality_issues = _qr["issues"] or None
                                    if not _qr["passed"]:
                                        # 对账续跑路径不重排 seed 重试，仅记录事件供人工处理
                                        _write_event(db, job, "job.quality_warning", {
                                            "asset_id": _a["asset_id"],
                                            "issues": _qr["issues"],
                                        })
                            # 分镜图出图质检（Phase 4）：仅 keyframe 类型，委托 apply_keyframe_image_qc
                            # 双层检查：提示词级（prompt_qc 降级告警）+ 图像级（多模态 vision 评图），失败不阻断
                            if job.shot_id:
                                _shot = db.get(Shot, job.shot_id)
                                if _shot is not None:
                                    _prompt = (job.params or {}).get("prompt") or ""
                                    _plan_block = ""
                                    _comp = (job.input_snapshot or {}).get("prompt_compilation") or {}
                                    if isinstance(_comp, dict):
                                        _plan_block = _comp.get("plan_posture_block") or ""
                                    _kf_assets = [a for a in created_assets if a.get("asset_type") == "keyframe"]
                                    for _kf in _kf_assets:
                                        _kf_path = _kf.get("file_path")
                                        if not _kf_path or not _prompt:
                                            continue
                                        try:
                                            from app.services.keyframe_image_qc import apply_keyframe_image_qc
                                            _kf_qc = apply_keyframe_image_qc(
                                                db, _shot, _prompt, _kf_path, plan_posture_block=_plan_block,
                                            )
                                            if _kf_qc.issues:
                                                _write_event(db, job, "job.quality_warning", {
                                                    "asset_id": _kf.get("asset_id"),
                                                    "qc_type": "keyframe_image_qc",
                                                    "prompt_qc_ok": _kf_qc.prompt_qc_ok,
                                                    "image_qc_ok": _kf_qc.image_qc_ok,
                                                    "issues": [{"code": i.code, "message": i.message} for i in _kf_qc.issues],
                                                })
                                        except Exception as _kfe:  # noqa: BLE001
                                            _write_event(db, job, "job.quality_warning", {
                                                "asset_id": _kf.get("asset_id"),
                                                "qc_type": "keyframe_image_qc",
                                                "error": f"{type(_kfe).__name__}: {str(_kfe)[:200]}",
                                            })
                            # 资产库 regenerate 原地覆盖闸口：引用镜头待重做 + 公共资产重置审核
                            if _regen_target is not None:
                                _apply_regenerate_gates(db, job, _regen_target)
                            # 成功终态
                            print(f"[worker] 任务成功 {job.id} ({job.job_type}) shot={job.shot_id} assets={len(created_assets)}", flush=True)
                            job.status = "succeeded"
                            job.finished_at = _now()
                            job.result = {"assets": created_assets, "prompt_id": job.prompt_id, "seed": seed}
                            _write_event(db, job, "job.succeeded", {"assets": len(created_assets)})
                            # 定义状态回写：与主成功路径一致，绑定正式资产并置为已生成。
                            # 此前该路径（对账续跑）漏掉这一步，导致任务成功但资产计划
                            # 一直停留在 pending，前端资产库看不到已生成状态。
                            try:
                                from app.services.asset_plan import sync_plan_after_generate
                                sync_plan_after_generate(db, job, created_assets)
                            except Exception as _pe:  # noqa: BLE001
                                _write_event(db, job, "asset_plan.sync_failed", {"error": str(_pe)[:200]})
                            # clip 产物 ffprobe 检测：回写时长与音频来源（compose 对齐依赖）
                            _probe_clip_and_update_shot(db, job, created_assets)
                            # 音频生成任务：片段状态回写（音频模块改造）
                            _sync_audio_segment_after_generate(db, job, created_assets)
                            # P0：实际尾帧落库（对账续跑路径同样需要绑定尾帧）
                            _persist_shot_last_frame(db, job, created_assets)
                            # 镜头状态派生（统一口径：见 services/shot_state.py）
                            if job.shot_id:
                                _shot = db.get(Shot, job.shot_id)
                                if _shot is not None and _shot.status == "generating":
                                    from app.services.shot_state import shot_ready
                                    _shot.status = "done" if shot_ready(db, _shot) else "pending"
                            db.commit()
                            return {"ok": True, "reason": "reconciled"}
                        elif done and err:
                            code, _, msg = err.partition(":")
                            _fail_job(db, job, code, msg)
                            return {"ok": False, "reason": code}
                    # history 为 None：任务尚未完成（仍在远端排队或执行中）。
                    # 对于已提交的任务（submission_state=submitted），不能降级重新提交——
                    # 否则同一镜头重复入队远端浪费算力。改为打 resume_prompt 标记并重入队，
                    # 下轮调度再次尝试续跑。
                    if job.submission_state == "submitted":
                        _snap = dict(job.input_snapshot or {})
                        _snap["resume_prompt"] = True
                        job.input_snapshot = _snap
                        job.status = "queued"
                        _write_event(db, job, "job.resume_deferred", {
                            "reason": "remote_still_running",
                            "prompt_id": job.prompt_id,
                        })
                        db.commit()
                        return {"ok": False, "reason": "resume_deferred", "retriable": True}
                    # 非已提交任务（submission_unknown / resume_prompt）：降级走正常流程
                    # history 为 None 或实例不可达：降级走正常流程重新提交
                except ComfyUIError:
                    if job.submission_state == "submitted":
                        # 续跑兜底（D-25）：ComfyUI 不可达时最多重试 3 次，
                        # 超过上限直接 fail，防止无限重试耗尽调度资源
                        _resume_attempts = (job.input_snapshot or {}).get("resume_attempts", 0)
                        if _resume_attempts >= 3:
                            _fail_job(db, job, "comfy_unreachable",
                                      f"ComfyUI 实例不可达，续跑已达最大重试次数（{_resume_attempts}）")
                            return {"ok": False, "reason": "comfy_unreachable"}
                        _snap = dict(job.input_snapshot or {})
                        _snap["resume_prompt"] = True
                        _snap["resume_attempts"] = _resume_attempts + 1
                        job.input_snapshot = _snap
                        job.status = "queued"
                        _write_event(db, job, "job.resume_deferred", {
                            "reason": "instance_unreachable",
                            "prompt_id": job.prompt_id,
                            "resume_attempt": _resume_attempts + 1,
                        })
                        db.commit()
                        return {"ok": False, "reason": "resume_deferred", "retriable": True}
                    pass  # 实例不可达：降级走正常流程

        # 0. 从实例池获取客户端（按实时负载调度 least-connections + 故障转移，D-19/D-21）；
        # Mock 模式跳过（D-11）。exclude_instances 为前次尝试失败的实例（任务级故障转移）。
        client = None
        used_instance = None
        if not settings.render_mock:
            # 同镜头亲和性（D-22）：优先复用同镜头最近成功任务的实例
            preferred_inst = _preferred_instance_for_shot(db, job, exclude_instances)
            try:
                client, used_instance = comfyui_pool.acquire_client_by_load(
                    preferred=preferred_inst, exclude=exclude_instances
                )
            except ComfyUIError as e:
                # 取不到客户端 ≠ 任务失败（2026-11-24 修复）。
                # 原实现直接 _fail_job()，于是「实例整机掉线」时排队中的任务
                # 一个接一个立刻判死，job_max_attempts=3 完全没机会生效。
                # 实测 E2E prj_7717c671b15c：06:56:32~34 的 1 秒内 4 个 job
                # 全部 attempts=1 就 failed（comfy_unreachable），而当时只是
                # trycloudflare 隧道整机掉了。
                # 现在按 job_max_attempts 重新入队，并把失败实例记进
                # exclude_instances 供下轮故障转移；达上限才真正判失败。
                if e.code == "comfy_unreachable":
                    _max_attempts = int(getattr(settings, "job_max_attempts", 3) or 3)
                    if (job.attempts or 0) < _max_attempts:
                        job.status = "queued"
                        _write_event(db, job, "job.retry", {
                            "stage": "acquire_client",
                            "error_code": e.code,
                            "error_message": e.message,
                            "attempt": job.attempts or 0,
                            "max_attempts": _max_attempts,
                        })
                        db.commit()
                        return {"ok": False, "reason": "comfy_unreachable", "retriable": True}
                _fail_job(db, job, e.code, e.message)
                return {"ok": False, "reason": e.code}

        # 兼容历史 Windows 落库的反斜杠路径（data\media\x.png）：
        # stored_path 统一归一为正斜杠；否则 Linux 上 open 会按字面反斜杠解析并报 ENOENT，
        # 所有参考图上传都会退化为 comfy_execution_error（本机 136 个资产均为该形态）。
        # 注意：本函数后段存在同名局部变量 stored_path（产物落盘 Path），
        # 直接引用模块级同名函数会触发 UnboundLocalError，故用别名局部导入。
        from app.core.config import stored_path as _norm_stored_path
        media_files = {
            _k: (str(_norm_stored_path(_v)) if _v else _v) for _k, _v in media_files.items()
        }
        # 1. 上传媒体（事务外；Mock 模式跳过）
        uploaded = {}
        if not settings.render_mock:
            try:
                for name, local_path in media_files.items():
                    uploaded[name] = client.upload_image(local_path)
            except ComfyUIError as e:
                # 提交前阶段失败：还有未尝试实例时换实例重试（D-21 任务级故障转移）
                remaining = [
                    i for i in comfyui_pool.enabled_instances()
                    if i["base_url"] not in exclude_instances
                    and used_instance is not None
                    and i["base_url"] != used_instance["base_url"]
                ]
                if used_instance is not None and remaining:
                    _write_event(db, job, "job.retry", {
                        "stage": "upload", "error_code": e.code,
                        "failed_instance": used_instance["base_url"],
                    })
                    db.commit()
                    return {"ok": False, "reason": e.code, "retriable": True,
                            "exclude_instances": exclude_instances + [used_instance["base_url"]]}
                _fail_job(db, job, e.code, e.message)
                return {"ok": False, "reason": e.code}
        else:
            # Mock 模式跳过上传，但媒体槽位仍需回填占位值（文件名），
            # 否则 prune_unbound_media_inputs 会剔除全部媒体槽位，
            # 误报 missing_reference_image（mock 与 prune 门禁不兼容的根因）
            import os as _os
            uploaded = {name: _os.path.basename(path) for name, path in media_files.items()}
        params.update(uploaded)

        # 2. 注入参数（P4 整改：先剔除未注入媒体的参考图节点及连线，
        # 防止工作流内置占位图 placeholder_*.png 污染生成结果）
        try:
            from app.services.workflow_inject import (
                compact_qwen_image_slots,
                compact_reference_slots,
                parse_workflow,
                prune_unbound_media_inputs,
                remap_image_tags,
                remap_picture_refs,
            )
            wf_definition, _pruned = prune_unbound_media_inputs(wf_record.definition, params)
            if _pruned:
                # 规范：参考图槽位全空 → 阻止提交并提示缺少参考图片（不允许静默生成）。
                # 工作流声明的媒体槽位全部被剔除时，说明未提供任何参考图。
                _parsed_wf = parse_workflow(wf_record.definition)
                _media_slots = sorted(
                    name for name, binding in _parsed_wf.params.items() if binding.media
                )
                if _media_slots and all(name in _pruned for name in _media_slots):
                    _fail_job(
                        db, job, "missing_reference_image",
                        "参考图槽位全部为空（" + ", ".join(_media_slots) + "），已阻止提交：请至少提供一张参考图片",
                    )
                    return {"ok": False, "reason": "missing_reference_image"}
                _write_event(db, job, "job.media_pruned", {"pruned_slots": _pruned})
                db.commit()
            # P0 整改：剔除槽位后 ref_images.ref_image_N 会出现断档，
            # 先压缩为连续编号，再按同一映射重写提示词中的 <Picture N>，
            # 保证模型收到的图片顺序与提示词描述严格一致（不重映射会整段错位）。
            wf_definition, _slot_remap = compact_reference_slots(wf_definition)
            _prompt_key = "prompt"
            _remap_info = None
            # clip 任务的图序以**执行期实际 media_files** 为准（2026-11-21）。
            #
            # 提交时算出的图序在两种情况下会过期，且两种都会让提示词与模型收到的图
            # 整体错位一位：
            #   · 提交时上一镜还没出尾帧 → 执行期 chained / last_frame_asset_id 补上；
            #   · independent 模式反而会 pop 掉 ref_prev_frame → 尾帧消失。
            # 现有 remap_picture_refs 只会「删行」，不会「插入并整体平移」，
            # 所以这里用与页面/提交完全相同的 reference_layout 口径按实况重算。
            _relayout_done = False
            _clip_shot = (
                db.get(Shot, job.shot_id)
                if job.job_type == "generate_clip" and job.shot_id
                else None
            )
            if (
                _clip_shot is not None
                and isinstance(params.get(_prompt_key), str)
            ):
                from app.services.reference_layout import (
                    compressed_numbers,
                    rebuild_prompt,
                )
                from app.services.ref_inject import workflow_ref_syntax
                from app.services.workflow.capabilities import declared_reference_slots

                _decl = declared_reference_slots(wf_record.capabilities)
                _live_slots = [
                    s for s in media_files
                    if s and (s.startswith("ref_") or s.startswith("first_frame"))
                ]
                _nums = compressed_numbers(_live_slots, _decl)
                if _nums:
                    # 槽位 → 资产名：media 里的路径可能用 / 或 \（历史数据混用），
                    # 直接当 SQL 条件会匹配不到 → 职责块退化成「@角色」，模型拿不到
                    # 角色名。这里在本项目资产里按归一化路径比对。
                    import os as _os2

                    _want = {
                        _os2.path.basename(str(media_files.get(s) or "").replace("\\", "/")): s
                        for s in _nums
                        if media_files.get(s)
                    }
                    _names: dict[str, str] = {}
                    if _want:
                        # 先本项目，再全库兜底：正常情况下参考图资产必属本项目，
                        # 但历史数据里存在跨项目引用，查不到就退化成「@角色」，
                        # 模型拿不到角色名会自由发挥长相。
                        def _collect(q) -> None:
                            for _a in q:
                                _bn = _os2.path.basename(
                                    str(_a.file_path or "").replace("\\", "/")
                                )
                                _slot = _want.get(_bn)
                                if _slot and _a.name and _slot not in _names:
                                    _names[_slot] = _a.name

                        _collect(
                            db.query(Asset)
                            .filter(Asset.project_id == job.project_id)
                            .order_by(Asset.version.desc())
                        )
                        _missing = [s for s in _nums if s not in _names]
                        if _missing:
                            _collect(db.query(Asset).order_by(Asset.version.desc()))
                    _before = params[_prompt_key]
                    params[_prompt_key] = rebuild_prompt(
                        _before, _nums, _names, _decl, workflow_ref_syntax(_decl)
                    )
                    _relayout_done = True
                    # 执行期重算后必须**实时回写**详情页数据源（2026-11-21）。
                    #
                    # 提交时算的图序在执行期可能已经变了（串行链式补上上一镜尾帧、
                    # 或并行模式 pop 掉尾帧），此时提示词与槽位都按实况重算过。
                    # 若只改内存里的 params 而不回写，用户点开视频详情看到的仍是
                    # 提交时那份 —— 与实际提交给 ComfyUI 的不是同一份，
                    # 「槽位对不上提示词」的观感就是这么来的。
                    from app.services.submission_persist import persist_submission

                    _persist = persist_submission(
                        db,
                        shot=_clip_shot,
                        project_id=job.project_id,
                        target="clip",
                        prompt=params[_prompt_key],
                        media=dict(media_files),
                        declared_slots=_decl,
                        workflow_id=job.workflow_id,
                        workflow_version=job.workflow_version,
                        negative_prompt=params.get("negative"),
                        parameters={
                            k: v for k, v in params.items()
                            if not str(k).startswith("__") and k not in ("prompt", "negative")
                        },
                    )
                    if params[_prompt_key] != _before or _persist.get("references"):
                        _write_event(db, job, "job.picture_relayout", {
                            "slots": dict(_nums),
                            "persisted": _persist,
                            "note": "执行期实际注入槽位与提交时不同，已按实况重算图序并回写详情页",
                        })
                        db.commit()
            if not _relayout_done and _slot_remap and isinstance(params.get(_prompt_key), str):
                _new_prompt, _dropped, _removed_lines = remap_picture_refs(
                    params[_prompt_key], _slot_remap
                )
                if _dropped:
                    _remap_info = {
                        "slot_remap": {k: dict(v) for k, v in _slot_remap.items()},
                        "dropped_pictures": _dropped,
                        "removed_lines": _removed_lines,
                    }
                    _write_event(db, job, "job.picture_remap", _remap_info)
                params[_prompt_key] = _new_prompt
                db.commit()
            elif _slot_remap:
                _remap_info = {"slot_remap": {k: dict(v) for k, v in _slot_remap.items()}}
            # P0 修复（Qwen 多参考图身份错位）：剔除槽位后 images.image_N 断档，
            # autogrow 按连线顺序解释图片身份，断档会让道具槽（如 image_6）
            # 被当成次角色槽解释（麦袋被当角色参考、角色身份错乱）。
            # 与 H3 处理一致：先压缩为连续编号，再按同一映射重写提示词 <imageN>。
            wf_definition, _img_remap = compact_qwen_image_slots(wf_definition)
            if _img_remap and isinstance(params.get(_prompt_key), str):
                _new_prompt2, _dropped_imgs = remap_image_tags(params[_prompt_key], _img_remap)
                if _dropped_imgs:
                    _img_info = {
                        "image_remap": {k: dict(v) for k, v in _img_remap.items()},
                        "dropped_images": _dropped_imgs,
                    }
                    _write_event(db, job, "job.image_remap", _img_info)
                params[_prompt_key] = _new_prompt2
                db.commit()
            # 防复发：任务参数的键必须与工作流节点标记声明的参数名一致。
            # 历史故障：# audio 任务曾把 speaker 写成 voice，而工作流声明的是 tts_voice，
            # 注入时找不到该参数即静默跳过、沿用画布默认值，导致选任何音色都出同一个声音。
            try:
                _declared = set(parse_workflow(wf_record.definition).params.keys())
                _undeclared = sorted(
                    k for k in params.keys()
                    if not k.startswith("__") and k not in _declared
                )
                if _undeclared:
                    _write_event(
                        db,
                        job,
                        "job.undeclared_params",
                        {
                            "undeclared": _undeclared,
                            "declared": sorted(_declared),
                            "note": "这些参数不会被注入，工作流将沿用画布默认值",
                        },
                    )
                    db.commit()
            except ValueError:
                # 标记校验失败交由下方 inject_params 统一报错，此处不重复处理
                pass
            injected_wf, snapshot = inject_params(wf_definition, params, seed=seed)
            if _slot_remap or _remap_info or _img_remap:
                _snap = dict(job.input_snapshot or {})
                _snap["reference_layout"] = {
                    "pruned_slots": _pruned,
                    "slot_remap": {k: dict(v) for k, v in _slot_remap.items()},
                    "image_remap": {k: dict(v) for k, v in _img_remap.items()},
                }
                _snap["params"] = snapshot
                job.input_snapshot = _snap
                db.commit()
        except ValueError as e:
            _fail_job(db, job, "workflow_marker_missing", str(e))
            return {"ok": False, "reason": "inject_failed"}

        if settings.render_mock:
            # Mock 渲染（D-11）：跳过提交/轮询/下载，直接生成占位产物，任务照常走完整状态机
            from app.services.mock_render import mock_outputs
            output_map = find_output_nodes(wf_record.definition)
            try:
                downloaded = mock_outputs(output_map, job.id)
            except Exception as e:
                _fail_job(db, job, "mock_render_failed", str(e))
                return {"ok": False, "reason": "mock_render_failed"}
            if not job.request_key:
                import uuid
                job.request_key = uuid.uuid4().hex
            prompt_id = f"mock-{job.id[-8:]}"
            job.prompt_id = prompt_id
            job.submission_state = "submitted"
            job.input_snapshot = {**(job.input_snapshot or {}), "render_mock": True}
            _write_event(db, job, "job.submitted", {"prompt_id": prompt_id, "mock": True})
            db.commit()
        else:
            # 3. 提交（提交前持久化 request_key 与意图）
            if not job.request_key:
                import uuid
                job.request_key = uuid.uuid4().hex
            job.submission_state = "submitting"
            job.input_snapshot = {**(job.input_snapshot or {}), "params": snapshot, "workflow_version": wf_record.version, "comfyui_instance": used_instance["base_url"]}
            db.commit()
            try:
                # 任务级 client_id：提交超时时可按它回查远端是否已接收该任务，
                # 避免共享 client_id 时把别的任务误认成本任务
                _cid = f"director-lab:{job.id}"
                prompt_id = client.submit_prompt(injected_wf, client_id=_cid)
            except ComfyUIError as e:
                job.submission_state = "submission_unknown"
                db.commit()
                # 提交请求超时：远端可能已接收并入队。先按 client_id 回查队列与历史，
                # 命中即续跑原任务，不重复提交（避免算力浪费与锚定图指针孤儿）
                prompt_id = None
                if e.code == "comfy_timeout":
                    try:
                        prompt_id = client.find_recent_prompt(_cid)
                    except ComfyUIError:
                        prompt_id = None
                    if prompt_id:
                        _write_event(db, job, "job.submit_reconciled", {
                            "prompt_id": prompt_id,
                            "instance": (used_instance or {}).get("base_url"),
                            "note": "提交超时但远端已接收，改走续跑",
                        })
                        db.commit()
                if prompt_id is None:
                    # 提交失败：还有未尝试实例时换实例重试（D-21 任务级故障转移）
                    remaining = [
                        i for i in comfyui_pool.enabled_instances()
                        if i["base_url"] not in exclude_instances
                        and used_instance is not None
                        and i["base_url"] != used_instance["base_url"]
                    ]
                    if used_instance is not None and remaining:
                        _write_event(db, job, "job.retry", {
                            "stage": "submit", "error_code": e.code,
                            "failed_instance": used_instance["base_url"],
                        })
                        db.commit()
                        return {"ok": False, "reason": e.code, "retriable": True,
                                "exclude_instances": exclude_instances + [used_instance["base_url"]]}
                    _fail_job(db, job, e.code, f"提交失败: {e.message}")
                    return {"ok": False, "reason": e.code}
            job.prompt_id = prompt_id
            job.submission_state = "submitted"
            _write_event(db, job, "job.submitted", {"prompt_id": prompt_id})
            db.commit()

            _task_progress(db, job.id, 30)
            # 4. 等待完成（事务外，超时按 comfy_timeout 处理）
            # WebSocket 优先：订阅 /ws 事件流接收进度并写 job.progress 事件；
            # WS 断连/不可用时自动降级为 HTTP 轮询（与旧行为一致）。
            import time
            deadline = time.time() + settings.comfyui_job_timeout_seconds
            history = None
            done, err = False, None
            _last_progress = {"value": None}

            def _on_progress(p: dict) -> None:
                # 进度回调：value 变化时写 job.progress 事件。
                # 必须使用独立 Session：进度回调由 WebSocket 线程/事件流触发，
                # 若复用主 Session 且 commit 中途失败，主事务会进入 rollback 状态，
                # 后续 db.refresh/commit 全部抛 “transaction has been rolled back”，
                # 最终把已经在 ComfyUI 渲染成功的任务误判为 failed。
                if p.get("value") == _last_progress["value"]:
                    return
                _last_progress["value"] = p.get("value")
                _pdb = None
                try:
                    _pdb = _db.SessionLocal()
                    _pjob = _pdb.get(Job, job.id)
                    if _pjob is not None:
                        _write_event(_pdb, _pjob, "job.progress", {
                            "value": p.get("value"),
                            "max": p.get("max"),
                            "node": p.get("node"),
                        })
                        _pdb.commit()
                except Exception:
                    pass  # 进度事件失败不阻断任务执行
                finally:
                    if _pdb is not None:
                        try:
                            _pdb.close()
                        except Exception:
                            pass

            _conn_failures = 0  # 连续连接失败计数：实例被删/断连时快速失败，不再等待总超时
            _poll_err_desc = ''  # 最近一次轮询请求异常描述（仅请求报错时赋值）
            while time.time() < deadline:
                db.refresh(job)
                if job.status == "cancelled":
                    _shot = db.get(Shot, job.shot_id) if job.shot_id else None
                    if _shot is not None and _shot.status == "generating":
                        _shot.status = "pending"
                        db.commit()
                    return {"ok": False, "reason": "cancelled"}
                # HTTP 轮询等待窗口（替代 WebSocket，不受 WS 断连影响）：取消检查间隔与总截止的较小值
                window_end = min(deadline, time.time() + 15.0)
                try:
                    done, err = client.wait_for_job_http(prompt_id, on_progress=_on_progress, poll_deadline=window_end)
                except ComfyUIError:
                    done, err = False, None
                if done:
                    break
                # HTTP 轮询未确认终态：检查实例可达性并兜底确认
                # 关键区分：请求报错表示实例可能不可达；请求成功但无历史表示任务仍在执行。
                # 后者不能计入失联次数，否则实例高负载时会被误判 comfy_unreachable。
                _poll_failed = False
                try:
                    history = client.get_history(prompt_id)
                except ComfyUIError as _pe:
                    history = None
                    _poll_failed = True
                    _poll_err_desc = f'{getattr(_pe, "code", "comfy_error")}: {getattr(_pe, "message", str(_pe))}'[:300]
                if not _poll_failed:
                    # 请求成功（实例可达）：失联计数归零；有历史再判断是否终态
                    _conn_failures = 0
                    if history is not None:
                        done, err = client.is_job_done(history)
                        if done:
                            break
                else:
                    _conn_failures += 1
                    if _conn_failures >= 5:
                        # 实例失联快速失败（被删除/连接中断）：不再等待总超时
                        # 阈值 5 次 + 指数退避：需持续失联约 2 分钟才判定，滤除高负载瞬时抖动
                        _inst_url = used_instance["base_url"] if used_instance else ""
                        _still_enabled = any(
                            i["base_url"] == _inst_url for i in comfyui_pool.enabled_instances()
                        )
                        if not _still_enabled:
                            _fail_job(db, job, "comfy_unreachable",
                                      f"实例已从实例池移除，任务终止：{_inst_url}")
                            return {"ok": False, "reason": "comfy_unreachable"}
                        # 实例仍在池中但连不上：可重试故障转移（换实例重新提交）
                        _remaining = [
                            i for i in comfyui_pool.enabled_instances()
                            if i["base_url"] not in exclude_instances
                            and i["base_url"] != _inst_url
                        ]
                        _write_event(db, job, "job.retry", {
                            "stage": "wait", "error_code": "comfy_unreachable",
                            "failed_instance": _inst_url,
                            "consecutive_failures": _conn_failures,
                        })
                        if _remaining:
                            db.commit()
                            return {"ok": False, "reason": "comfy_unreachable", "retriable": True,
                                    "exclude_instances": exclude_instances + [_inst_url]}
                        # 二次确认：单实例无备用时，先查远端队列与历史，核实任务是否仍在执行。
                        # 高负载下 history 请求超时并不等于实例失联，直接判失败会产生孤儿任务白跑算力。
                        _remote_alive = False
                        try:
                            _qc = client.get_queue()
                            _q_ids = [x[1] for x in _qc.get('queue_running', [])] + [
                                x[1] for x in _qc.get('queue_pending', [])
                            ]
                            if prompt_id in _q_ids:
                                _remote_alive = True
                            else:
                                _hc = client.get_history(prompt_id)
                                if _hc is None:
                                    # 既不在队列也查不到历史：可能刚结束待写入，保守按仍存活处理
                                    _remote_alive = True
                                else:
                                    history = _hc
                        except Exception:  # noqa: BLE001
                            _remote_alive = False
                        if _remote_alive:
                            _write_event(db, job, 'job.wait_extended', {
                                'stage': 'wait',
                                'reason': 'remote_still_alive',
                                'consecutive_failures': _conn_failures,
                                'last_error': _poll_err_desc,
                            })
                            db.commit()
                            _conn_failures = 0
                            time.sleep(settings.comfyui_poll_interval_seconds)
                            continue
                        _fail_job(db, job, "comfy_unreachable",
                                  f"实例连接中断且无其他可用实例：{_inst_url}")
                        return {"ok": False, "reason": "comfy_unreachable"}
                    # 退避间隔：连续连接失败时指数退避（5s→10s→20s→30s，30s 封顶），
                    # 给高负载实例恢复窗口，避免瞬时抖动累计过快触发误判
                    _backoff = min(5 * (2 ** (_conn_failures - 1)), 30)
                    time.sleep(_backoff)
                    continue
                time.sleep(settings.comfyui_poll_interval_seconds)
            # WS 确认完成后拉取 history 用于提取产物
            if history is None:
                for _ in range(3):
                    try:
                        history = client.get_history(prompt_id)
                    except ComfyUIError:
                        history = None
                    if history is not None:
                        break
                    time.sleep(1.0)
            if history is None:
                if not done:
                    _fail_job(db, job, "comfy_timeout", f"等待超时（{settings.comfyui_job_timeout_seconds}s）")
                    return {"ok": False, "reason": "comfy_timeout"}
                _fail_job(db, job, "output_missing", "任务已完成但无法获取产物历史")
                return {"ok": False, "reason": "output_missing"}
            done, err = client.is_job_done(history)
            if not done:
                _fail_job(db, job, "comfy_timeout", "等待超时未完成")
                return {"ok": False, "reason": "comfy_timeout"}
            if err:
                db.refresh(job)
                if job.status == "cancelled":
                    _shot = db.get(Shot, job.shot_id) if job.shot_id else None
                    if _shot is not None and _shot.status == "generating":
                        _shot.status = "pending"
                        db.commit()
                    return {"ok": False, "reason": "cancelled"}
                code, _, msg = err.partition(":")
                _fail_job(db, job, code, msg)
                return {"ok": False, "reason": code}

            # 5. 提取并下载产物（事务外）
            outputs = client.extract_outputs(history)
            output_map = find_output_nodes(wf_record.definition)
            downloaded = []
            try:
                for out_name, node_id in output_map.items():
                    items = outputs.get(node_id, [])
                    if not items:
                        _fail_job(db, job, "output_missing", f"输出 {out_name}（节点 {node_id}）未产出文件")
                        return {"ok": False, "reason": "output_missing"}
                    for item in items:
                        content = client.download_file(item["filename"], item.get("subfolder", ""), item.get("type", "output"))
                        downloaded.append({"out_name": out_name, "item": item, "content": content})
            except ComfyUIError as e:
                _fail_job(db, job, e.code, e.message)
                return {"ok": False, "reason": e.code}

        _task_progress(db, job.id, 90)
        # 6. 资产落库（{asset_id}_{version}.{ext} 重命名）
        from app.models import ids as id_gen
        import os
        media_dir = settings.media_dir
        media_dir.mkdir(parents=True, exist_ok=True)
        asset_type_map = {"cover": "keyframe", "clip": "clip", "voice": "audio", "bgm": "audio", "sfx": "audio"}
        created_assets = []
        _regen_target = _asset_regenerate_target(db, job)
        for rec in downloaded:
            out_name = rec["out_name"]
            filename = rec["item"]["filename"]
            ext = os.path.splitext(filename)[1].lstrip(".") or "bin"
            asset_type = asset_type_map.get(out_name, "keyframe")
            # 定妆图/基准图/锚定图按实体归属落 character/scene/prop（否则前端资产库分类永远查不到）
            if asset_type == "keyframe" and _entity_asset_type:
                asset_type = _entity_asset_type
            if _regen_target is not None:
                # 资产库 regenerate：原地覆盖原文件，名称与位置固定不变
                filepath = _overwrite_asset_file(_regen_target, rec["content"])
                try:
                    from app.services.thumbnail import make_thumb
                    _regen_target.thumb_path = make_thumb(
                        filepath, os.path.splitext(os.path.basename(filepath))[0]
                    )
                except Exception:
                    pass
                _rt_snap = dict(_regen_target.input_snapshot or {})
                _rt_snap["regenerated_at"] = _now().isoformat()
                _rt_snap["regenerate_job_id"] = job.id
                _regen_target.input_snapshot = _rt_snap
                created_assets.append({
                    "asset_id": _regen_target.id, "asset_type": _regen_target.asset_type,
                    "out_name": out_name, "file_path": filepath,
                })
                continue
            if asset_type == "keyframe" and job.shot_id:
                _shot = db.get(Shot, job.shot_id)
                if _shot is not None:
                    created_assets.append(_store_shot_keyframe(db, job, _shot, rec["content"], ext))
                    continue
            # 分镜视频与分镜图同口径：每镜只保留一份，重新生成原地覆盖旧视频
            if asset_type == "clip" and job.shot_id:
                _shot = db.get(Shot, job.shot_id)
                if _shot is not None:
                    created_assets.append(_store_shot_clip(db, job, _shot, rec["content"], ext))
                    continue
            # 资产图（角色定妆图/场景基准图/道具锚定图）重新生成：
            # 已有同名 current 资产时原地覆盖并清掉历史，资产库只保留一份
            if _entity_asset_type and _entity_asset_name:
                _overwritten = _store_entity_asset(
                    db, job, _entity_asset_type, _entity_asset_name, rec["content"], ext
                )
                if _overwritten is not None:
                    created_assets.append(_overwritten)
                    continue
            version = _next_asset_version(db, job, asset_type, _entity_asset_name)
            # 分镜图重新生成直接覆盖当前版本，不保留候选版本概念
            _supersede_previous_versions(db, job, asset_type, _entity_asset_name)
            aid = id_gen.asset_id()
            stored_name = f"{aid}_v{version}.{ext}"
            stored_path = media_dir / stored_name
            with open(stored_path, "wb") as f:
                f.write(rec["content"])
            # 缩略图生成（D-17）：失败不阻断落库，thumb_path 置 None
            from app.services.thumbnail import make_thumb
            thumb_path = make_thumb(str(stored_path), f"{aid}_v{version}")
            asset = Asset(
                id=aid,
                project_id=job.project_id,
                asset_type=asset_type,
                name=(
                    _entity_asset_name
                    if (_entity_asset_name and _entity_asset_type)
                    else f"{out_name}_{job.id[-6:]}"
                ),
                version=version,
                file_path=str(stored_path),
                thumb_path=thumb_path,
                source_job_id=job.id,
                validity="current",
                input_snapshot=job.input_snapshot,
            )
            db.add(asset)
            db.flush()
            created_assets.append({
                "asset_id": aid, "asset_type": asset_type,
                "out_name": out_name, "file_path": str(stored_path),
            })

        # 资产库 regenerate 原地覆盖闸口：引用镜头待重做 + 公共资产重置审核
        if _regen_target is not None:
            _apply_regenerate_gates(db, job, _regen_target)
        # 7.0 锚定图/定妆图/基准图回写（D-20）：generate 端点将 __prop_id__ / __character_id__ / __scene_id__
        # 写入 job.params，生成成功后将首个实体产物回写到对应记录
        _anchor_map = [
            ("__prop_id__", Prop, "anchor_asset_id"),
            ("__character_id__", Character, "portrait_asset_id"),
            ("__scene_id__", Scene, "baseline_asset_id"),
        ]
        _kf_asset = next(
            (a for a in created_assets if a["asset_type"] in ("keyframe", "character", "scene", "prop")),
            None,
        )
        # 产物即为当前生效版本，实体主图指针同步跟随
        if _kf_asset is not None:
            for _key, _model, _field in _anchor_map:
                _tid = (job.params or {}).get(_key)
                if not _tid:
                    continue
                _obj = db.get(_model, _tid)
                if _obj is not None:
                    setattr(_obj, _field, _kf_asset["asset_id"])
        # 6.5 图片质量检查（P3）：图片类产物自动验收；不合格换 seed 重试，超限标记失败
        # Mock 模式产物为占位图（带边框文字），跳过检查以保证演示与端到端测试可用
        if not settings.render_mock:
            _IMAGE_TYPES = ("keyframe", "character", "scene", "prop")
            _img_assets = [a for a in created_assets if a.get("asset_type") in _IMAGE_TYPES]
            _quality_failed = []
            for _a in _img_assets:
                try:
                    from app.services.image_quality import check_image_quality
                    _qr = check_image_quality(_a["file_path"], asset_type=_a["asset_type"])
                except Exception as _qe:  # noqa: BLE001
                    _qr = {"passed": False, "issues": [f"质量检查异常: {type(_qe).__name__}"], "checks": {}}
                _qa = db.get(Asset, _a["asset_id"])
                if _qa is not None:
                    _qa.quality_status = "passed" if _qr["passed"] else "failed"
                    _qa.quality_issues = _qr["issues"] or None
                if not _qr["passed"]:
                    if _regen_target is not None and _a["asset_id"] == _regen_target.id:
                        # 原地覆盖资产无候选版本可回退，不进入换 seed 重试，仅记录告警事件
                        _write_event(db, job, "job.quality_warning", {
                            "asset_id": _a["asset_id"], "issues": _qr["issues"],
                        })
                    else:
                        _quality_failed.append({"asset_id": _a["asset_id"], "issues": _qr["issues"]})
            if _quality_failed:
                _max_attempts = settings.job_max_attempts
                if (job.attempts or 0) < _max_attempts:
                    _requeue_for_quality_retry(db, job, _quality_failed)
                    return {"ok": False, "reason": "quality_check_failed", "retriable": True}
                _fail_job(db, job, "quality_check_failed",
                          f"图片质量检查不合格（已重试 {job.attempts} 次）: {_quality_failed}")
                return {"ok": False, "reason": "quality_check_failed"}
        # 6.6 分镜图出图质检（Phase 4）：仅 keyframe 类型，委托 apply_keyframe_image_qc
        # 双层检查：提示词级（prompt_qc 降级告警）+ 图像级（多模态 vision 评图），失败不阻断
        if job.shot_id:
            _shot = db.get(Shot, job.shot_id)
            if _shot is not None:
                _prompt = (job.params or {}).get("prompt") or ""
                _plan_block = ""
                _comp = (job.input_snapshot or {}).get("prompt_compilation") or {}
                if isinstance(_comp, dict):
                    _plan_block = _comp.get("plan_posture_block") or ""
                _kf_assets = [a for a in created_assets if a.get("asset_type") == "keyframe"]
                for _kf in _kf_assets:
                    _kf_path = _kf.get("file_path")
                    if not _kf_path or not _prompt:
                        continue
                    try:
                        from app.services.keyframe_image_qc import apply_keyframe_image_qc
                        _kf_qc = apply_keyframe_image_qc(
                            db, _shot, _prompt, _kf_path, plan_posture_block=_plan_block,
                        )
                        if _kf_qc.issues:
                            _write_event(db, job, "job.quality_warning", {
                                "asset_id": _kf.get("asset_id"),
                                "qc_type": "keyframe_image_qc",
                                "prompt_qc_ok": _kf_qc.prompt_qc_ok,
                                "image_qc_ok": _kf_qc.image_qc_ok,
                                "issues": [{"code": i.code, "message": i.message} for i in _kf_qc.issues],
                            })
                    except Exception as _kfe:  # noqa: BLE001
                        _write_event(db, job, "job.quality_warning", {
                            "asset_id": _kf.get("asset_id"),
                            "qc_type": "keyframe_image_qc",
                            "error": f"{type(_kfe).__name__}: {str(_kfe)[:200]}",
                        })
        # 7. 成功终态
        print(f"[worker] 任务成功 {job.id} ({job.job_type}) shot={job.shot_id} assets={len(created_assets)}", flush=True)
        job.status = "succeeded"
        job.result = {"assets": created_assets, "prompt_id": prompt_id, "seed": seed}
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded", "result": job.result})
        # 定义状态回写：绑定正式资产并置为已生成，名称与占位路径保持不变
        try:
            from app.services.asset_plan import sync_plan_after_generate
            sync_plan_after_generate(db, job, created_assets)
        except Exception as _pe:  # noqa: BLE001
            _write_event(db, job, "asset_plan.sync_failed", {"error": str(_pe)[:200]})
        # clip 产物 ffprobe 检测：回写时长与音频来源（compose 对齐依赖）
        _probe_clip_and_update_shot(db, job, created_assets)
        # 音频生成任务：片段状态回写（音频模块改造）
        _sync_audio_segment_after_generate(db, job, created_assets)
        # P0：从本次实际生成视频提取末帧并绑定 shot.last_frame_asset_id，
        # 供后续镜头首帧/尾帧引用；绑定结果决定是否释放链式下一环
        if job.job_type == "generate_clip":
            _write_event(db, job, "chain.extracting_last_frame", {
                "note": "正在提取本镜尾帧，作为下一镜头首帧引用；串行链式执行中，请勿中断或重新发起批量",
            })
        _last_frame_bound = _persist_shot_last_frame(db, job, created_assets)
        # 链式生成（chained）：仅当尾帧已成功绑定时才释放下一环；
        # 绑定失败则级联取消后续任务，避免下一镜在缺少首帧参数的情况下进入队列
        if job.job_type == "generate_clip" and not _last_frame_bound:
            _write_event(db, job, "chain.last_frame_missing", {
                "note": "尾帧提取/绑定失败，为避免下一镜缺首帧参数，链式后续任务已取消；可修复后重新发起批量",
            })
            try:
                _cancel_chained_after_failure(db, job, "last_frame_extract_failed")
            except Exception as _ce:  # noqa: BLE001
                _write_event(db, job, "chain.cancel_failed", {"error": str(_ce)[:200]})
        else:
            try:
                _release_chained_next(db, job)
            except Exception as _ce:  # noqa: BLE001
                _write_event(db, job, "chain.release_failed", {"error": str(_ce)[:200]})
        # 7.5 镜头状态派生（§3.2/§4.2）：必需产物齐全 → done，否则回 pending（阶段内部分完成是正常状态）
        # 口径统一在 services/shot_state.py：H3 VA 内嵌音轨即满足对白/旁白镜头的音频必需性
        if job.shot_id:
            _shot = db.get(Shot, job.shot_id)
            if _shot is not None:
                from app.services.shot_state import shot_ready
                _shot.status = "done" if shot_ready(db, _shot) else "pending"

                # 跨集连续性：记录首帧使用（对应改造第五/八节）
                if job.job_type == "generate_clip":
                    try:
                        from app.services.continuity import record_opening_frame_usage
                        _scene = db.get(Scene, _shot.scene_id)
                        if _scene is not None:
                            _is_first = (
                                db.query(Shot)
                                .join(Scene, Shot.scene_id == Scene.id)
                                .filter(Scene.project_id == _scene.project_id, Shot.episode == _shot.episode)
                                .order_by(Scene.seq, Shot.seq)
                                .first()
                            )
                            if _is_first is not None and _is_first.id == _shot.id and _shot.episode > 1:
                                # __media__ 已被 pop 到 media_files，从 media_files 读取首帧路径
                                _ff_path = media_files.get("first_frame") or media_files.get("ref_prev_frame")
                                if _ff_path:
                                    _clip_asset = next(
                                        (a for a in created_assets if a.get("asset_type") == "clip"),
                                        None,
                                    )
                                    record_opening_frame_usage(
                                        db, job.project_id, _shot.episode,
                                        _ff_path,
                                        asset_id=_clip_asset["asset_id"] if _clip_asset else None,
                                    )
                    except Exception:
                        pass  # 连续性记录失败不影响生成结果
        db.commit()
        return {"ok": True, "assets": created_assets, "seed": seed}
    except Exception as e:  # 兜底：未预期异常按执行错误记录
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None and job.status not in ("succeeded", "failed"):
                # 已提交到 ComfyUI 的任务（有 prompt_id 且 submission_state=submitted）
                # 不因本地异常判失败：远端可能仍在渲染或已渲染成功，
                # 交回队列由续跑/对账分支接管下载产物，
                # 否则会出现「视频已在 ComfyUI 生成好、前端却一直标红失败」。
                # attempts 上限是死循环的最后一道闸：若异常根因未消除而反复重入队，
                # 超过阈值必须真正判失败，避免同一任务被无限次重复执行。
                if (
                    job.prompt_id
                    and job.submission_state == "submitted"
                ):
                    if job.attempts >= settings.job_max_attempts:
                        _fail_job(db, job, "comfy_execution_error",
                                  f"执行器异常（已达重试上限 {settings.job_max_attempts} 次）: {str(e)[:200]}")
                    else:
                        _snap = dict(job.input_snapshot or {})
                        _snap["resume_prompt"] = True
                        job.input_snapshot = _snap
                        job.status = "queued"
                        job.error_code = None
                        job.error_msg = None
                        job.finished_at = None
                        _write_event(db, job, "job.requeued_after_exception", {
                            "error": str(e)[:300],
                            "prompt_id": job.prompt_id,
                        })
                        db.commit()
                else:
                    _fail_job(db, job, "comfy_execution_error", f"执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
