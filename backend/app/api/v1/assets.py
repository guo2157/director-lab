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
from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.database import get_db
from app.models import Asset, AssetRef, Job, Shot, ids
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
        q = q.filter(Asset.asset_type == asset_type)
    if status:
        q = q.filter(Asset.validity == status)
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
    project_id: str = None,
    asset_type: str = "reference",
    name: str = None,
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    if not project_id:
        raise errors.AppError("invalid_request", "project_id 必填", 400)
    data = await file.read()
    if len(data) > 500 * 1024 * 1024:
        raise errors.AppError("invalid_request", "单文件上限 500MB", 400)
    a = Asset(
        id=ids.asset_id(),
        project_id=project_id,
        asset_type=asset_type,
        name=name or (file.filename or "upload"),
        version=1,
        validity="current",
        input_snapshot={"created_at": _now().isoformat()},
    )
    media_dir = Path(settings.media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix or ".bin"
    dest = media_dir / f"{a.id}_v1{suffix}"
    dest.write_bytes(data)
    a.file_path = str(dest)
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
