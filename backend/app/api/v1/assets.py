"""资产管理接口：API.md §6 字段级契约。
- GET /api/assets：资产列表（project_id 必填，分页）
- POST /api/assets/upload：上传资产（multipart，单文件 ≤500MB）
- GET /api/assets/{id}：资产详情（含 versions 与 referenced_by）
- POST /api/assets/{id}/lock|unlock：锁定/解锁
- POST /api/assets/{id}/rollback：回滚版本（生成新版本，镜头标记待重做）
- DELETE /api/assets/{id}：删除（被引用 409 asset_in_use，已锁定 409 asset_locked）
- POST /api/assets/batch/delete|lock|unlock：批量操作（整批校验，不部分执行）
- POST /api/assets/export：打包导出（zip 流）
- GET /api/assets/{id}/versions：版本时间线
"""
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.database import get_db
from app.models import Asset, AssetRef, Character, Job, Prop, Scene, Shot, ids
from app.models.enums import ASSET_TYPE
from app.services import errors

router = APIRouter(prefix="/api/assets", tags=["assets"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_asset(db: Session, asset_id: str) -> Asset:
    a = db.get(Asset, asset_id)
    if a is None:
        raise errors.AppError("asset_not_found", "资产不存在", 404)
    return a


def _to_out(a: Asset) -> dict:
    return {
        "id": a.id,
        "project_id": a.project_id,
        "asset_type": a.asset_type,
        "name": a.name,
        "version": a.version,
        "parent_id": a.parent_id,
        "file_path": a.file_path,
        "thumb_path": a.thumb_path,
        "url": f"/api/assets/{a.id}/file",
        "thumb_url": f"/api/assets/{a.id}/file?thumb=true" if a.thumb_path else None,
        "source_job_id": a.source_job_id,
        "ref_count": a.ref_count,
        "locked": bool(a.locked),
        "validity": a.validity,
        "created_at": a.input_snapshot.get("created_at") if a.input_snapshot else None,
    }


@router.get("")
def list_assets(
    project_id: str,
    asset_type: str | None = None,
    status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(Asset).filter(Asset.project_id == project_id)
    if asset_type:
        # 非法分类值直接 422，禁止静默返回错误类别的数据（分类正确性要求）
        if asset_type not in ASSET_TYPE:
            allowed = "、".join(ASSET_TYPE)
            raise errors.AppError("invalid_asset_type", f"asset_type 取值无效，允许值：{allowed}", 422)
        q = q.filter(Asset.asset_type == asset_type)
    # 默认只返回当前有效资产（原地覆盖模型下每个实体只应展示 1 张）；
    # 需要查看历史行时显式传 status=stale 或 status=all
    if status:
        if status != "all":
            q = q.filter(Asset.validity == status)
    else:
        q = q.filter(Asset.validity == "current")
    if keyword:
        q = q.filter(Asset.name.contains(keyword))
    total = q.count()
    items = (
        q.order_by(Asset.id.desc())
        .offset((max(page, 1) - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": [_to_out(a) for a in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{asset_id}/file")
def get_asset_file(asset_id: str, thumb: bool = False, db: Session = Depends(get_db)) -> FileResponse:
    """媒体文件服务：返回资产原文件或缩略图（thumb=true 且缩略图存在时）。
    缩略图缺失时回退原文件，保证前端展示不中断。"""
    a = _get_asset(db, asset_id)
    path_str = a.file_path
    if thumb and a.thumb_path:
        path_str = a.thumb_path
    path = Path(path_str)
    if not path.exists():
        raise errors.AppError("asset_file_missing", "资产文件不存在或已被清理", 404)
    import mimetypes
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.post("/upload", status_code=201)
async def upload_asset(
    file: UploadFile,
    project_id: str = Form(None),
    asset_type: str = Form(None),
    name: str = Form(None),
    replace_asset_id: str = Form(None),
    db: Session = Depends(get_db),
) -> dict:
    """上传资产：asset_type 必填且必须命中权威枚举，保证资产能落到正确的分类分组。

    - replace_asset_id 提供时直接走替换语义（生成 vN+1，旧版本转 superseded）
    - 不再提供游离于枚举外的兜底类型：缺失或非法一律 422，避免产出无法分类的资产
    """
    if not project_id:
        raise errors.AppError("invalid_request", "project_id 必填", 400)
    if replace_asset_id:
        return await replace_asset(replace_asset_id, file, db)
    allowed = "、".join(ASSET_TYPE)
    if not asset_type:
        raise errors.AppError("invalid_asset_type", f"asset_type 必填，允许值：{allowed}", 422)
    if asset_type not in ASSET_TYPE:
        raise errors.AppError("invalid_asset_type", f"asset_type 取值无效，允许值：{allowed}", 422)
    settings = get_settings()
    data = await file.read()
    if not data:
        raise errors.AppError("invalid_request", "上传文件为空", 400)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise errors.AppError("invalid_request", "单文件上限 500MB", 400)
    suffix = Path(file.filename or "").suffix or ".bin"
    final_type = asset_type
    _check_file_type(final_type, suffix)
    asset_name = name or (file.filename or "upload")
    version = _next_version_by_name(db, project_id, final_type, asset_name)
    a = Asset(
        id=ids.asset_id(),
        project_id=project_id,
        asset_type=final_type,
        name=asset_name,
        version=version,
        validity="current",
        input_snapshot={"created_at": _now().isoformat()},
    )
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    dest = media_dir / f"{a.id}_v{version}{suffix}"
    dest.write_bytes(data)
    a.file_path = str(dest)
    a.thumb_path = _make_thumb_safe(str(dest), f"{a.id}_v{version}")
    db.add(a)
    db.commit()
    return _to_out(a)


@router.get("/batch/delete", include_in_schema=False)
def _batch_delete_guard():
    raise errors.AppError("invalid_request", "方法不允许", 405)


@router.post("/batch/delete")
def batch_delete_assets(payload: dict, db: Session = Depends(get_db)) -> dict:
    asset_ids = payload.get("ids") or payload.get("asset_ids") or []
    assets = []
    for aid in asset_ids:
        a = _get_asset(db, aid)
        if a.locked:
            raise errors.AppError("asset_locked", f"资产 {aid} 已锁定", 409)
        if a.ref_count > 0:
            raise errors.AppError("asset_in_use", f"资产 {aid} 被引用中", 409)
        assets.append(a)
    for a in assets:
        db.delete(a)
    db.commit()
    return {"deleted": len(assets)}


@router.post("/batch/lock")
def batch_lock_assets(payload: dict, db: Session = Depends(get_db)) -> dict:
    asset_ids = payload.get("ids") or payload.get("asset_ids") or []
    n = 0
    for aid in asset_ids:
        a = _get_asset(db, aid)
        if not a.locked:
            a.locked = True
            n += 1
    db.commit()
    return {"locked": n}


@router.post("/batch/unlock")
def batch_unlock_assets(payload: dict, db: Session = Depends(get_db)) -> dict:
    asset_ids = payload.get("ids") or payload.get("asset_ids") or []
    n = 0
    for aid in asset_ids:
        a = _get_asset(db, aid)
        if a.locked:
            a.locked = False
            n += 1
    db.commit()
    return {"unlocked": n}


@router.post("/export")
def export_assets(payload: dict, db: Session = Depends(get_db)):
    asset_ids = payload.get("ids") or payload.get("asset_ids") or []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for aid in asset_ids:
            a = _get_asset(db, aid)
            if a.file_path and Path(a.file_path).exists():
                zf.write(a.file_path, arcname=Path(a.file_path).name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="assets_export.zip"'},
    )


@router.get("/{asset_id}")
def get_asset(asset_id: str, db: Session = Depends(get_db)) -> dict:
    a = _get_asset(db, asset_id)
    versions = (
        db.query(Asset)
        .filter(Asset.project_id == a.project_id, Asset.name == a.name)
        .order_by(Asset.version)
        .all()
    )
    refs = db.query(AssetRef).filter(AssetRef.asset_id == a.id).all()
    shot_ids = list({r.shot_id for r in refs})
    shots = [db.get(Shot, sid) for sid in shot_ids]
    out = _to_out(a)
    out["versions"] = [
        {"version": v.version, "id": v.id, "validity": v.validity, "parent_id": v.parent_id}
        for v in versions
    ]
    out["referenced_by"] = [
        {"shot_id": s.id, "shot_number": getattr(s, "shot_number", None)}
        for s in shots
        if s is not None
    ]
    return out


@router.get("/{asset_id}/versions")
def list_asset_versions(asset_id: str, db: Session = Depends(get_db)) -> list[dict]:
    a = _get_asset(db, asset_id)
    versions = (
        db.query(Asset)
        .filter(Asset.project_id == a.project_id, Asset.name == a.name)
        .order_by(Asset.version)
        .all()
    )
    return [
        {
            "version": v.version,
            "id": v.id,
            "validity": v.validity,
            "parent_id": v.parent_id,
            "file_path": v.file_path,
        }
        for v in versions
    ]


@router.post("/{asset_id}/lock")
def lock_asset(asset_id: str, db: Session = Depends(get_db)) -> dict:
    a = _get_asset(db, asset_id)
    a.locked = True
    db.commit()
    return _to_out(a)


@router.post("/{asset_id}/unlock")
def unlock_asset(asset_id: str, db: Session = Depends(get_db)) -> dict:
    a = _get_asset(db, asset_id)
    a.locked = False
    db.commit()
    return _to_out(a)


@router.post("/{asset_id}/rollback")
def rollback_asset(asset_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    a = _get_asset(db, asset_id)
    target_version = payload.get("target_version")
    if target_version is None:
        raise errors.AppError("invalid_request", "target_version 必填", 400)
    target = (
        db.query(Asset)
        .filter(
            Asset.project_id == a.project_id,
            Asset.name == a.name,
            Asset.version == target_version,
        )
        .first()
    )
    if target is None:
        raise errors.AppError("asset_not_found", f"版本 {target_version} 不存在", 404)
    max_version = (
        db.query(Asset)
        .filter(Asset.project_id == a.project_id, Asset.name == a.name)
        .order_by(Asset.version.desc())
        .first()
        .version
    )
    new_asset = Asset(
        id=ids.asset_id(),
        project_id=a.project_id,
        asset_type=a.asset_type,
        name=a.name,
        version=max_version + 1,
        parent_id=target.id,
        file_path=target.file_path,
        thumb_path=target.thumb_path,
        validity="current",
        input_snapshot={"created_at": _now().isoformat(), "rollback_from": target_version},
    )
    a.validity = "superseded"
    db.add(new_asset)
    # 引用该资产的镜头标记待重做（不自动触发任务）
    refs = db.query(AssetRef).filter(AssetRef.asset_id == a.id).all()
    for r in refs:
        shot = db.get(Shot, r.shot_id)
        if shot is not None and hasattr(shot, "redo_required"):
            shot.redo_required = True
    db.commit()
    return _to_out(new_asset)


# ── 上传 / 替换 / 重新生成：共享校验与工具 ────────────────────────────────
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}
_ALLOWED_EXT = _IMAGE_EXT | _VIDEO_EXT
_IMAGE_ASSET_TYPES = {"character", "scene", "prop", "keyframe", "last_frame"}
_VIDEO_ASSET_TYPES = {"clip"}
_REGENERATABLE_TYPES = {"character", "scene", "prop"}
# 镜头产物类型：与镜头关联依赖 source_job_id，替换时需继承
_SHOT_BOUND_TYPES = {"keyframe", "clip", "last_frame"}
_ENTITY_KEY = {"character": "__character_id__", "scene": "__scene_id__", "prop": "__prop_id__"}


def _check_file_type(asset_type: str, suffix: str) -> None:
    """扩展名白名单 + 与资产类型匹配校验：图片类只收图片、视频类只收视频。"""
    ext = (suffix or "").lower()
    if ext not in _ALLOWED_EXT:
        allowed = "、".join(sorted(_ALLOWED_EXT))
        raise errors.AppError(
            "invalid_file_type",
            f"不支持的文件类型 {ext or '(无扩展名)'}，允许：{allowed}",
            422,
        )
    if asset_type in _VIDEO_ASSET_TYPES and ext not in _VIDEO_EXT:
        raise errors.AppError("invalid_file_type", "视频类资产只接受 mp4/webm/mov/mkv 文件", 422)
    if asset_type in _IMAGE_ASSET_TYPES and ext not in _IMAGE_EXT:
        raise errors.AppError("invalid_file_type", "图片类资产只接受 png/jpg/jpeg/webp/bmp 文件", 422)


def _make_thumb_safe(file_path: str, stem: str) -> str | None:
    """生成缩略图，失败不阻断落库（前端回退原文件展示）。"""
    try:
        from app.services.thumbnail import make_thumb
        return make_thumb(file_path, stem)
    except Exception:
        return None


def _next_version_by_name(db: Session, project_id: str, asset_type: str, name: str) -> int:
    """同一 项目+类型+名称 范围内版本号递增，无历史则为 1。"""
    latest = (
        db.query(func.max(Asset.version))
        .filter(
            Asset.project_id == project_id,
            Asset.asset_type == asset_type,
            Asset.name == name,
        )
        .scalar()
    )
    return int(latest or 0) + 1


def _sync_entity_pointer(db: Session, old_id: str, new_id: str, asset_type: str) -> list[str]:
    """实体主图指针同步：仅当指针正好指向被替换版本时才切换到新版本。"""
    changed: list[str] = []
    if asset_type == "character":
        for ch in db.query(Character).filter(Character.portrait_asset_id == old_id).all():
            ch.portrait_asset_id = new_id
            changed.append(ch.id)
    elif asset_type == "scene":
        for sc in db.query(Scene).filter(Scene.baseline_asset_id == old_id).all():
            sc.baseline_asset_id = new_id
            changed.append(sc.id)
    elif asset_type == "prop":
        for pr in db.query(Prop).filter(Prop.anchor_asset_id == old_id).all():
            pr.anchor_asset_id = new_id
            changed.append(pr.id)
    return changed


def _mark_referencing_shots(db: Session, asset_id: str) -> list[str]:
    """引用该资产的镜头标记待重做（只标记，不自动触发任务）。"""
    shot_ids: list[str] = []
    for ref in db.query(AssetRef).filter(AssetRef.asset_id == asset_id).all():
        shot = db.get(Shot, ref.shot_id)
        if shot is None:
            continue
        if hasattr(shot, "redo_required"):
            shot.redo_required = True
        if hasattr(shot, "keyframe_confirmed"):
            shot.keyframe_confirmed = False
        shot_ids.append(shot.id)
    return shot_ids


def _entity_for_asset(db: Session, asset: Asset):
    """按实体主图指针反查归属实体，指针未命中时回退按名称匹配。"""
    if asset.asset_type == "character":
        obj = db.query(Character).filter(Character.portrait_asset_id == asset.id).first()
        if obj is None:
            obj = (
                db.query(Character)
                .filter(Character.project_id == asset.project_id, Character.name == asset.name)
                .first()
            )
        return obj
    if asset.asset_type == "scene":
        obj = db.query(Scene).filter(Scene.baseline_asset_id == asset.id).first()
        if obj is None:
            obj = (
                db.query(Scene)
                .filter(Scene.project_id == asset.project_id, Scene.title == asset.name)
                .first()
            )
        return obj
    if asset.asset_type == "prop":
        obj = db.query(Prop).filter(Prop.anchor_asset_id == asset.id).first()
        if obj is None:
            obj = (
                db.query(Prop)
                .filter(Prop.project_id == asset.project_id, Prop.name == asset.name)
                .first()
            )
        return obj
    return None


def _default_prompt_for(db: Session, asset: Asset, workflow=None) -> tuple[str, str]:
    """默认提示词统一由阶段 B 编译器产出（D-24），与生成端点同源。

    历史实现曾在此用 build_*_prompt 本地拼装，导致资产库「重新生成」弹窗预览的
    提示词与生成端点实际编译结果不一致，已统一收敛到 compile_asset_image。
    """
    from app.services.prompt_compiler import compile_asset_image

    ent = _entity_for_asset(db, asset)
    if ent is None:
        raise ValueError(
            f"asset_entity_missing: 未找到该资产对应的角色/场景/道具实体（{asset.asset_type}）"
        )
    compiled = compile_asset_image(db, asset.asset_type, ent.id, workflow=workflow)
    if compiled.status != "ready":
        first = compiled.blocking_issues[0]
        raise ValueError(f"{first['code']}: {first['message']}")
    return compiled.positive_prompt, compiled.negative_prompt


def _match_image_workflow(db: Session, workflow_id: str | None):
    """图片工作流匹配：显式 ID 优先，其次 默认 → 内置 → 任意。"""
    from app.models import Workflow
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.AppError("workflow_not_found", "工作流不存在", 404)
        return wf
    wf = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == "image", Workflow.is_default == True)  # noqa: E712
        .first()
    )
    if wf is None:
        wf = (
            db.query(Workflow)
            .filter(Workflow.workflow_type == "image", Workflow.is_builtin == True)  # noqa: E712
            .first()
        )
    if wf is None:
        wf = db.query(Workflow).filter(Workflow.workflow_type == "image").first()
    if wf is None:
        raise errors.AppError("workflow_not_found", "未找到可用的图片工作流", 404)
    return wf


@router.get("/{asset_id}/regenerate/config")
def get_asset_regenerate_config(asset_id: str, db: Session = Depends(get_db)) -> dict:
    """重新生成弹窗初值：默认沿用资产模板渲染的提示词，仅提示词可改。"""
    asset = _get_asset(db, asset_id)
    if asset.asset_type not in _REGENERATABLE_TYPES:
        raise errors.AppError(
            "invalid_request",
            f"仅角色/场景/道具资产支持在此重新生成，当前类型：{asset.asset_type}",
            422,
        )
    from app.services import asset_prompts
    try:
        prompt, negative = _default_prompt_for(db, asset)
    except ValueError as e:
        raise errors.AppError("invalid_request", str(e), 422)
    return {
        "asset_id": asset.id,
        "asset_type": asset.asset_type,
        "version": asset.version,
        "prompt": prompt,
        "negative_prompt": negative,
        "asset_prompt_version": asset_prompts.ASSET_PROMPTS_VERSION,
        "editable": {
            "prompt": True,
            "negative_prompt": True,
            "refs": False,
            "workflow": False,
            "params": False,
        },
    }


@router.post("/{asset_id}/replace", status_code=200)
async def replace_asset(asset_id: str, file: UploadFile, db: Session = Depends(get_db)) -> dict:
    """上传新文件原地替换资产（2026-10-28 改版）：直接覆盖原文件内容。

    固定约束：资产名称与文件路径（图片名称和图片位置）不可变——
    上传的图片直接替换现有文件，不生成新版本记录、不改变 file_path/name。
    版本时间线与回滚不适用于替换（无新版本产生）。
    """
    asset = _get_asset(db, asset_id)
    if asset.locked:
        raise errors.AppError("asset_locked", "资产已锁定，请先解锁再替换", 409)
    data = await file.read()
    if not data:
        raise errors.AppError("invalid_request", "上传文件为空", 400)
    if len(data) > _MAX_UPLOAD_BYTES:
        raise errors.AppError("invalid_request", "单文件上限 500MB", 400)
    suffix = Path(file.filename or "").suffix or ".bin"
    _check_file_type(asset.asset_type, suffix)
    # 原地覆盖：文件路径固定不变，直接写原 file_path
    dest = Path(asset.file_path)
    if not dest.is_absolute():
        # file_path 为相对后端工作目录的存储口径（data/media/xxx），按原样解析
        if not dest.exists():
            dest = Path(get_settings().media_dir) / dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    # 缩略图按原路径覆盖重生成
    asset.thumb_path = _make_thumb_safe(str(dest), dest.stem)
    asset.input_snapshot = {
        **(asset.input_snapshot or {}),
        "replaced_at": _now().isoformat(),
        "original_filename": file.filename,
    }
    # 更新 updated_at：文件内容已变化，前端以 v=updated_at 破图片缓存，必须变化才能拉到新图
    asset.updated_at = _now()
    affected_shots = _mark_referencing_shots(db, asset.id)
    # 人工审核闸口（D-23）：公共资产替换后原审核结论失效，需重新提交审核
    if asset.asset_type in ("character", "scene", "prop"):
        from app.services.assets_review import reset_assets_review
        reset_assets_review(db, asset.project_id)
    # 分镜图换图后该镜分镜图需重新人工确认（D-13 静图先行闸口）
    if asset.asset_type == "keyframe":
        from app.models import Shot as _Shot
        _src = db.get(Job, asset.source_job_id) if asset.source_job_id else None
        if _src is not None and _src.shot_id:
            _shot = db.get(_Shot, _src.shot_id)
            if _shot is not None:
                _shot.keyframe_confirmed = False
                affected_shots = list({*affected_shots, _shot.id})
    db.commit()
    out = _to_out(asset)
    out.update({
        "affected_shots": affected_shots,
    })
    return out


@router.post("/{asset_id}/regenerate", status_code=202)
def regenerate_asset(asset_id: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    """资产级重新生成：角色/场景/道具按可编辑提示词重跑图片工作流，结果落候选版本。"""
    asset = _get_asset(db, asset_id)
    if asset.asset_type not in _REGENERATABLE_TYPES:
        raise errors.AppError(
            "invalid_request",
            f"仅角色/场景/道具资产支持资产级重新生成，当前类型：{asset.asset_type}；"
            "分镜图/视频请在分镜重新生成工作台操作",
            422,
        )
    if asset.locked:
        raise errors.AppError("asset_locked", "资产已锁定，请先解锁再重新生成", 409)
    payload = payload or {}
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key:
        exist = db.query(Job).filter(Job.request_key == idempotency_key).first()
        if exist is not None:
            return {
                "job_id": exist.id,
                "status": exist.status,
                "asset_id": asset.id,
                "base_version": asset.version,
                "idempotent": True,
            }
    prompt = (payload.get("prompt") or "").strip()
    negative = (payload.get("negative_prompt") or "").strip()
    prompt_edited = bool(prompt)
    if not prompt:
        try:
            default_prompt, default_negative = _default_prompt_for(db, asset)
        except ValueError as e:
            raise errors.AppError("invalid_request", str(e), 422)
        prompt = default_prompt
        negative = negative or default_negative
    ent = _entity_for_asset(db, asset)
    if ent is None:
        raise errors.AppError(
            "asset_entity_missing",
            "未找到该资产对应的角色/场景/道具实体，无法重新生成",
            409,
        )
    wf = _match_image_workflow(db, payload.get("workflow_id"))
    from app.services import asset_prompts
    params: dict = {"prompt": prompt, "__asset_prompt_version__": asset_prompts.ASSET_PROMPTS_VERSION}
    if negative:
        params["negative"] = negative
    params[_ENTITY_KEY[asset.asset_type]] = ent.id
    job = Job(
        id=ids.job_id(),
        project_id=asset.project_id,
        shot_id=None,
        job_type="generate_keyframe",
        workflow_id=wf.id,
        workflow_version=wf.version,
        params=params,
        status="queued",
        # 用户手工发起的历史版本刷新优先于批量排队的生成任务（与重新生成分镜视频同口径）：
        # 否则一次大批量出视频会让这次重新生成排在几十个任务之后，半天看不到结果
        priority=100,
        request_key=idempotency_key,
        created_at=_now(),
        input_snapshot={
            "workflow_version": wf.version,
            "params": params,
            "generation_mode": "asset_regenerate",
            "regeneration": {
                "asset_id": asset.id,
                "asset_type": asset.asset_type,
                "base_version": asset.version,
                "entity_id": ent.id,
                "prompt_edited": prompt_edited,
            },
        },
    )
    db.add(job)
    db.commit()
    return {
        "job_id": job.id,
        "status": job.status,
        "asset_id": asset.id,
        "base_version": asset.version,
        "workflow_id": wf.id,
        "workflow_version": wf.version,
        "prompt_edited": prompt_edited,
    }


@router.delete("/{asset_id}", status_code=204)
def delete_asset(asset_id: str, db: Session = Depends(get_db)) -> None:
    a = _get_asset(db, asset_id)
    if a.locked:
        raise errors.AppError("asset_locked", "资产已锁定，不可删除", 409)
    if a.ref_count > 0:
        raise errors.AppError("asset_in_use", "资产被引用中，不可删除", 409)
    db.delete(a)
    db.commit()
    return None
