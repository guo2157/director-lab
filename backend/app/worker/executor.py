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
from app.models import Asset, Job, JobEvent, Shot, Workflow
from app.services.workflow_inject import find_output_nodes, inject_params


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    """事件与状态同事务写入 outbox。event_seq 由数据库自增，并回写 job.event_seq 供增量补齐。"""
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
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


def execute_generate_job(job_id: str) -> dict:
    """执行一个生成类任务。返回执行结果摘要（供 worker 记录）。"""
    settings = get_settings()
    # 运行时查找 SessionLocal：跟随 app.core.database 的当前绑定（测试替换引擎时保持一致）
    db: Session = _db.SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status not in ("queued", "submission_unknown"):
            return {"ok": False, "reason": f"invalid_status:{job.status}"}

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

        wf_record = db.get(Workflow, job.workflow_id) if job.workflow_id else None
        if wf_record is None:
            _fail_job(db, job, "no_matching_workflow", "任务未关联工作流")
            return {"ok": False, "reason": "no_workflow"}

        params = dict(job.params or {})
        media_files = params.pop("__media__", {})  # {param_name: local_file_path}
        seed = params.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)  # random 策略：本 job 内所有种子共用
        params["seed"] = seed

        # 0. 从实例池获取客户端（加权轮询负载均衡 + 故障转移）；Mock 模式跳过（D-11）
        client = None
        used_instance = None
        if not settings.render_mock:
            try:
                client, used_instance = comfyui_pool.acquire_client()
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
                _fail_job(db, job, e.code, e.message)
                return {"ok": False, "reason": e.code}
        params.update(uploaded)

        # 2. 注入参数
        try:
            injected_wf, snapshot = inject_params(wf_record.definition, params, seed=seed)
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
                _fail_job(db, job, e.code, f"提交失败: {e.message}")
                return {"ok": False, "reason": e.code}
            job.prompt_id = prompt_id
            job.submission_state = "submitted"
            _write_event(db, job, "job.submitted", {"prompt_id": prompt_id})
            db.commit()

            # 4. 轮询（事务外，超时按 comfy_timeout 处理）
            import time
            deadline = time.time() + settings.comfyui_job_timeout_seconds
            history = None
            while time.time() < deadline:
                db.refresh(job)
                if job.status == "cancelled":
                    _shot = db.get(Shot, job.shot_id) if job.shot_id else None
                    if _shot is not None and _shot.status == "generating":
                        _shot.status = "pending"
                        db.commit()
                    return {"ok": False, "reason": "cancelled"}
                try:
                    history = client.get_history(prompt_id)
                except ComfyUIError:
                    history = None
                if history is not None:
                    done, err = client.is_job_done(history)
                    if done:
                        break
                time.sleep(settings.comfyui_poll_interval_seconds)
            if history is None:
                _fail_job(db, job, "comfy_timeout", f"轮询超时（{settings.comfyui_job_timeout_seconds}s）")
                return {"ok": False, "reason": "comfy_timeout"}
            done, err = client.is_job_done(history)
            if not done:
                _fail_job(db, job, "comfy_timeout", "轮询超时未完成")
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
        for rec in downloaded:
            out_name = rec["out_name"]
            filename = rec["item"]["filename"]
            ext = os.path.splitext(filename)[1].lstrip(".") or "bin"
            asset_type = asset_type_map.get(out_name, "keyframe")
            version = 1
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
                name=f"{out_name}_{job.id[-6:]}",
                version=version,
                file_path=str(stored_path),
                thumb_path=thumb_path,
                source_job_id=job.id,
                validity="current",
                input_snapshot=job.input_snapshot,
            )
            db.add(asset)
            created_assets.append({"asset_id": aid, "asset_type": asset_type, "file_path": str(stored_path)})

        # 7.0 锚定图/定妆图/基准图回写（D-20）：generate 端点将 __prop_id__ / __character_id__ / __scene_id__
        # 写入 job.params，生成成功后将首个 keyframe 产物回写到对应记录
        from app.models import Character, Prop, Scene
        _anchor_map = [
            ("__prop_id__", Prop, "anchor_asset_id"),
            ("__character_id__", Character, "portrait_asset_id"),
            ("__scene_id__", Scene, "baseline_asset_id"),
        ]
        _kf_asset = next((a for a in created_assets if a["asset_type"] == "keyframe"), None)
        if _kf_asset is not None:
            for _key, _model, _field in _anchor_map:
                _tid = (job.params or {}).get(_key)
                if not _tid:
                    continue
                _obj = db.get(_model, _tid)
                if _obj is not None:
                    setattr(_obj, _field, _kf_asset["asset_id"])
        # 7. 成功终态
        job.status = "succeeded"
        job.result = {"assets": created_assets, "prompt_id": prompt_id, "seed": seed}
        job.finished_at = _now()
        _write_event(db, job, "job.finished", {"status": "succeeded", "result": job.result})
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
