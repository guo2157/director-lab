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

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core import database as _db
from app.integrations.comfyui import ComfyUIClient, ComfyUIError
from app.integrations import comfyui_pool
from app.models import Asset, Character, Job, JobEvent, Project, Prop, Scene, Shot, Workflow
from app.services.workflow_inject import find_output_nodes, inject_params


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    """原地覆盖资产文件：file_path 固定不变，返回实际写入路径。"""
    from pathlib import Path

    dest = Path(asset.file_path)
    if not dest.is_absolute() and not dest.exists():
        dest = Path(get_settings().media_dir) / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return str(dest)


def _apply_regenerate_gates(db: Session, job: Job, asset: Asset) -> None:
    """资产库 regenerate 原地覆盖后的闸口（与 replace_asset 保持一致）：
    引用镜头标记待重做；公共资产重置人工审核。"""
    from app.models import AssetRef

    for ref in db.query(AssetRef).filter(AssetRef.asset_id == asset.id).all():
        shot = db.get(Shot, ref.shot_id)
        if shot is None:
            continue
        if hasattr(shot, "redo_required"):
            shot.redo_required = True
        if hasattr(shot, "keyframe_confirmed"):
            shot.keyframe_confirmed = False
    if asset.asset_type in ("character", "scene", "prop"):
        from app.services.assets_review import reset_assets_review

        reset_assets_review(db, job.project_id)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    """事件与状态同事务写入 outbox。event_seq 由数据库自增，并回写 job.event_seq 供增量补齐。"""
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq


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
    except Exception as e:  # noqa: BLE001
        try:
            _write_event(db, job, "clip.probe_failed", {"error": str(e)[:200]})
        except Exception:
            pass


def _persist_shot_last_frame(db: Session, job: Job, created_assets: list[dict]) -> None:
    """clip 成功后提取实际末帧并绑定到 shot.last_frame_asset_id（P0 整改）。

    整改前 shot.last_frame_asset_id 只有读取、没有任何写入点，
    导致后续镜头永远拿不到上一镜尾帧，镜间连续性实际未生效。
    尾帧必须来自本次生成视频实际解码的最后一帧（extract_last_frame 含黑屏回退），
    不使用分镜图或预估帧；提取失败只记录事件，不阻断任务成功。
    """
    if job.job_type != "generate_clip" or not job.shot_id:
        return
    clip = next((a for a in created_assets if a.get("asset_type") == "clip"), None)
    if clip is None:
        return
    shot = db.get(Shot, job.shot_id)
    if shot is None:
        return
    clip_asset = db.get(Asset, clip["asset_id"])
    if clip_asset is None:
        return
    try:
        from app.services.shot_chain import store_shot_last_frame
        result = store_shot_last_frame(db, shot, clip_asset, get_settings().media_dir)
    except Exception as e:  # noqa: BLE001
        _write_event(db, job, "shot.last_frame_failed", {
            "shot_id": shot.id,
            "error": f"{type(e).__name__}: {e}"[:300],
        })
        return
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
    重试；提交后阶段的错误（超时/执行错误）不换实例，保持原失败语义。
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
            job.status = "running"
            job.started_at = _now()
            job.attempts = (job.attempts or 0) + 1
            _write_event(db, job, "job.started", {"attempt": job.attempts})
            # 镜头状态流转：进入生成中（§3.2）
            if job.shot_id:
                _shot = db.get(Shot, job.shot_id)
                if _shot is not None and _shot.status != "generating":
                    _shot.status = "generating"
            db.commit()

        # 统一资产就绪门禁二次校验（D-23 强化）：任务创建与真正执行之间存在时间差，
        # 执行前必须重新校验资产就绪状态，防止入队后资产被重新生成（审核回退 pending）
        # 或失效仍提交 ComfyUI。实体资产图（定妆/基准/锚定图）自身不适用该门禁。
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
                if "ref_prev_frame" not in media_files and _shot.last_frame_asset_id:
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
                _project = db.get(Project, job.project_id)
                if _project is not None and "width" not in params and "height" not in params:
                    _h3_wh = {"9:16": (736, 1312), "16:9": (1312, 736), "1:1": (768, 768)}
                    if _project.aspect_ratio in _h3_wh:
                        params["width"], params["height"] = _h3_wh[_project.aspect_ratio]
                if "length" not in params and _shot.duration:
                    _target = float(_shot.duration) * 24
                    _n = max(7, min(21, round((_target - 5) / 17)))
                    params["length"] = 17 * _n + 5
                # 持久化注入参数到 input_snapshot（可追溯：实际提交的生成参数与参考图）
                _snap = dict(job.input_snapshot or {})
                _snap["injected_params"] = {
                    k: params[k] for k in ("width", "height", "length") if k in params
                }
                _snap["injected_media"] = dict(media_files)
                job.input_snapshot = _snap
                db.commit()

        # 0. submission_unknown 续跑分支（D-23 修复）：已有 prompt_id 时跳过上传/提交，
        # 直接查远端 history 续跑下载，避免重新提交浪费算力。
        if not settings.render_mock and job.status == "submission_unknown" and job.prompt_id:
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
                                _is_regen_rec = bool((job.input_snapshot or {}).get("regeneration"))
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
                                asset_id = id_gen.asset_id()
                                # 版本号必须在产物类型与实体归属确定之后再计算，
                                # 否则会用到尚未赋值的类型变量，且实体类资产无法按名称隔离版本
                                version = _next_asset_version(db, job, asset_type, _entity_asset_name)
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
                                    validity=("candidate" if _is_regen_rec else "current"),
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
                                created_assets.append({"asset_id": asset_id, "out_name": out_name, "file_path": filepath})
                            # 锚定图回写（D-20）
                            _prop_id = (job.params or {}).get("__prop_id__")
                            _character_id = (job.params or {}).get("__character_id__")
                            _scene_id = (job.params or {}).get("__scene_id__")
                            if _prop_id:
                                _p = db.get(Prop, _prop_id)
                                if _p and not _p.anchor_asset_id and not _is_regen_rec:
                                    _kf = [a for a in created_assets if a["out_name"] == "cover"]
                                    if _kf:
                                        _p.anchor_asset_id = _kf[0]["asset_id"]
                            if _character_id:
                                _c = db.get(Character, _character_id)
                                if _c and not _c.portrait_asset_id and not _is_regen_rec:
                                    _kf = [a for a in created_assets if a["out_name"] == "cover"]
                                    if _kf:
                                        _c.portrait_asset_id = _kf[0]["asset_id"]
                            if _scene_id:
                                _s = db.get(Scene, _scene_id)
                                if _s and not _s.baseline_asset_id and not _is_regen_rec:
                                    _kf = [a for a in created_assets if a["out_name"] == "cover"]
                                    if _kf:
                                        _s.baseline_asset_id = _kf[0]["asset_id"]
                            # 资产库 regenerate 原地覆盖闸口：引用镜头待重做 + 公共资产重置审核
                            if _regen_target is not None:
                                _apply_regenerate_gates(db, job, _regen_target)
                            # 成功终态
                            print(f"[worker] 任务成功 {job.id} ({job.job_type}) shot={job.shot_id} assets={len(created_assets)}", flush=True)
                            job.status = "succeeded"
                            job.finished_at = _now()
                            job.result = {"assets": created_assets, "prompt_id": job.prompt_id, "seed": seed}
                            _write_event(db, job, "job.succeeded", {"assets": len(created_assets)})
                            # clip 产物 ffprobe 检测：回写时长与音频来源（compose 对齐依赖）
                            _probe_clip_and_update_shot(db, job, created_assets)
                            # P0：实际尾帧落库（对账续跑路径同样需要绑定尾帧）
                            _persist_shot_last_frame(db, job, created_assets)
                            # 镜头状态派生
                            if job.shot_id:
                                _shot = db.get(Shot, job.shot_id)
                                if _shot is not None and _shot.status == "generating":
                                    _all_jobs = db.query(Job).filter(Job.shot_id == job.shot_id).all()
                                    _has_kf = any(j.status == "succeeded" and j.job_type == "generate_keyframe" for j in _all_jobs)
                                    _has_clip = any(j.status == "succeeded" and j.job_type == "generate_clip" for j in _all_jobs)
                                    _has_audio = any(j.status == "succeeded" and j.job_type in ("generate_audio", "generate_bgm", "generate_sfx") for j in _all_jobs)
                                    if _shot.dialogue or _shot.narration:
                                        _shot.status = "done" if (_has_kf and _has_clip and _has_audio) else "pending"
                                    else:
                                        _shot.status = "done" if (_has_kf and _has_clip) else "pending"
                            db.commit()
                            return {"ok": True, "reason": "reconciled"}
                        elif done and err:
                            code, _, msg = err.partition(":")
                            _fail_job(db, job, code, msg)
                            return {"ok": False, "reason": code}
                    # history 为 None 或实例不可达：降级走正常流程重新提交
                except ComfyUIError:
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
                _fail_job(db, job, e.code, e.message)
                return {"ok": False, "reason": e.code}

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
        params.update(uploaded)

        # 2. 注入参数（P4 整改：先剔除未注入媒体的参考图节点及连线，
        # 防止工作流内置占位图 placeholder_*.png 污染生成结果）
        try:
            from app.services.workflow_inject import (
                compact_reference_slots,
                prune_unbound_media_inputs,
                remap_picture_refs,
            )
            wf_definition, _pruned = prune_unbound_media_inputs(wf_record.definition, params)
            if _pruned:
                _write_event(db, job, "job.media_pruned", {"pruned_slots": _pruned})
                db.commit()
            # P0 整改：剔除槽位后 ref_images.ref_image_N 会出现断档，
            # 先压缩为连续编号，再按同一映射重写提示词中的 <Picture N>，
            # 保证模型收到的图片顺序与提示词描述严格一致（不重映射会整段错位）。
            wf_definition, _slot_remap = compact_reference_slots(wf_definition)
            _prompt_key = "prompt"
            _remap_info = None
            if _slot_remap and isinstance(params.get(_prompt_key), str):
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
            injected_wf, snapshot = inject_params(wf_definition, params, seed=seed)
            if _slot_remap or _remap_info:
                _snap = dict(job.input_snapshot or {})
                _snap["reference_layout"] = {
                    "pruned_slots": _pruned,
                    "slot_remap": {k: dict(v) for k, v in _slot_remap.items()},
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
                prompt_id = client.submit_prompt(injected_wf)
            except ComfyUIError as e:
                job.submission_state = "submission_unknown"
                db.commit()
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

            # 4. 等待完成（事务外，超时按 comfy_timeout 处理）
            # WebSocket 优先：订阅 /ws 事件流接收进度并写 job.progress 事件；
            # WS 断连/不可用时自动降级为 HTTP 轮询（与旧行为一致）。
            import time
            deadline = time.time() + settings.comfyui_job_timeout_seconds
            history = None
            done, err = False, None
            _last_progress = {"value": None}

            def _on_progress(p: dict) -> None:
                # 进度回调：value 变化时写 job.progress 事件（事件与状态同事务）
                if p.get("value") == _last_progress["value"]:
                    return
                _last_progress["value"] = p.get("value")
                try:
                    _write_event(db, job, "job.progress", {
                        "value": p.get("value"),
                        "max": p.get("max"),
                        "node": p.get("node"),
                    })
                    db.commit()
                except Exception:
                    pass  # 进度事件失败不阻断任务执行

            while time.time() < deadline:
                db.refresh(job)
                if job.status == "cancelled":
                    _shot = db.get(Shot, job.shot_id) if job.shot_id else None
                    if _shot is not None and _shot.status == "generating":
                        _shot.status = "pending"
                        db.commit()
                    return {"ok": False, "reason": "cancelled"}
                # WS 等待窗口：取消检查间隔与总截止的较小值
                window_end = min(deadline, time.time() + 15.0)
                try:
                    done, err = client.wait_for_job_ws(prompt_id, on_progress=_on_progress, poll_deadline=window_end)
                except ComfyUIError:
                    done, err = False, None
                if done:
                    break
                # WS 未确认终态（断连/超时/无事件）：HTTP 轮询兜底
                try:
                    history = client.get_history(prompt_id)
                except ComfyUIError:
                    history = None
                if history is not None:
                    done, err = client.is_job_done(history)
                    if done:
                        break
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
            # 多图参考重新生成工作台 V1：重新生成任务落候选版本，
            # 版本号按同镜头同类型递增，不覆盖当前生效版本
            _is_regen = bool((job.input_snapshot or {}).get("regeneration"))
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
            version = _next_asset_version(db, job, asset_type, _entity_asset_name)
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
                validity=("candidate" if _is_regen else "current"),
                input_snapshot=job.input_snapshot,
            )
            db.add(asset)
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
        # 重新生成产物为候选版本，采纳前不得改动实体主图指针，否则详情页会指向未采纳的候选
        if _kf_asset is not None and not _is_regen:
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
                    # 不合格产物转历史版本，换 seed 重新生成
                    for _qf in _quality_failed:
                        _bad = db.get(Asset, _qf["asset_id"])
                        if _bad is not None:
                            _bad.validity = "superseded"
                    job.status = "queued"
                    if job.params:
                        job.params.pop("seed", None)
                    _write_event(db, job, "job.quality_retry", {
                        "attempt": job.attempts, "failed": _quality_failed,
                    })
                    db.commit()
                    return {"ok": False, "reason": "quality_check_failed", "retriable": True}
                _fail_job(db, job, "quality_check_failed",
                          f"图片质量检查不合格（已重试 {job.attempts} 次）: {_quality_failed}")
                return {"ok": False, "reason": "quality_check_failed"}
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
        # P0：从本次实际生成视频提取末帧并绑定 shot.last_frame_asset_id，
        # 供后续镜头首帧/尾帧引用；失败只记录事件，不阻断任务成功
        _persist_shot_last_frame(db, job, created_assets)
        # 7.5 镜头状态派生（§3.2/§4.2）：必需产物齐全 → done，否则回 pending（阶段内部分完成是正常状态）
        if job.shot_id:
            _shot = db.get(Shot, job.shot_id)
            if _shot is not None:
                def _shot_has(t):
                    return (
                        db.query(Asset)
                        .join(Job, Job.id == Asset.source_job_id)
                        .filter(Job.shot_id == _shot.id, Asset.asset_type == t,
                                Asset.validity == "current", Job.status == "succeeded")
                        .count() > 0
                    )
                _need_audio = _shot.shot_kind in ("dialogue", "narration")
                if _shot_has("keyframe") and _shot_has("clip") and (not _need_audio or _shot_has("audio")):
                    _shot.status = "done"
                else:
                    _shot.status = "pending"

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
                _fail_job(db, job, "comfy_execution_error", f"执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
