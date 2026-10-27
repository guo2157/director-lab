"""合成与导出接口：API.md §10 契约。
- POST /api/projects/{id}/compose：合成可预览成片（202 长任务）
- GET /api/projects/{id}/compositions：合成版本列表（created_at 降序，可按集筛选）
- GET /api/projects/{id}/compositions/{comp_id}：合成版本详情
- POST /api/projects/{id}/export：创建独立导出任务（202 长任务）
- GET /api/projects/{id}/exports：导出记录列表
- GET /api/exports/{id}/download：流式下载导出文件
纪律：项目非 active 返回 409；存在未审核放行的超容差镜头拒绝合成（422）；
导出不隐式触发重新合成，来源合成过期 409 composition_stale。
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import Composition, Export, Job, Project, Scene, Shot
from app.models import ids
from app.schemas.script import (
    ComposeCreate, CompositionDetailOut, CompositionOut, ExportCreate, ExportOut, ParseJobOut,
)
from app.services import errors

router = APIRouter(prefix="/api", tags=["compose"])


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise errors.project_not_found()
    return p


def _activate_if_draft(db: Session, p: Project) -> None:
    if p.status == "draft":
        p.status = "active"


def _comp_to_out(c: Composition) -> CompositionOut:
    return CompositionOut(
        composition_id=c.id, episode=c.episode, duration=c.duration, stale=c.stale,
        created_at=c.created_at, file_path=c.file_path, status=c.status,
    )


@router.post("/projects/{project_id}/compose", status_code=202)
def create_compose(project_id: str, payload: ComposeCreate,
                   db: Session = Depends(get_db)) -> ParseJobOut:
    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可合成")
    # 存在未审核放行的超容差镜头时整个合成请求被拒绝（422）
    blocking = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id, Shot.status == "review")
    )
    if payload.episode is not None:
        blocking = blocking.filter(Shot.episode == payload.episode)
    if blocking.count() > 0:
        raise errors.biz_error(
            code="duration_mismatch_review_required", http=422,
            message="存在未审核放行的超容差镜头，须先审核放行或重生成")
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="compose",
        params={
            "episode": payload.episode,
            "include_subtitle": payload.include_subtitle,
            "subtitle_mode": payload.subtitle_mode,
            "bgm_asset_id": payload.bgm_asset_id,
            "bgm_volume": payload.bgm_volume,
            "compose_tolerance": payload.compose_tolerance,
        },
        status="queued", created_at=_now_naive(),
    )
    db.add(job)
    _activate_if_draft(db, p)
    db.commit()
    return ParseJobOut(job_id=job.id, status=job.status)


@router.get("/projects/{project_id}/compositions")
def list_compositions(project_id: str, episode: int | None = Query(None),
                      db: Session = Depends(get_db)) -> dict:
    _get_project(db, project_id)
    q = db.query(Composition).filter(Composition.project_id == project_id)
    if episode is not None:
        q = q.filter(Composition.episode == episode)
    items = q.order_by(Composition.created_at.desc()).all()
    return {"total": len(items), "items": [_comp_to_out(c).model_dump(mode="json") for c in items]}


@router.get("/projects/{project_id}/compositions/{comp_id}")
def get_composition(project_id: str, comp_id: str,
                    db: Session = Depends(get_db)) -> CompositionDetailOut:
    _get_project(db, project_id)
    c = db.get(Composition, comp_id)
    if c is None or c.project_id != project_id:
        raise errors.invalid_request("合成版本不存在")
    job = db.query(Job).filter(Job.job_type == "compose", Job.result["composition_id"].as_string() == c.id).first()
    return CompositionDetailOut(
        composition_id=c.id, episode=c.episode, duration=c.duration, stale=c.stale,
        created_at=c.created_at, file_path=c.file_path, status=c.status,
        source_snapshot=c.source_snapshot, warnings=c.warnings,
        job_id=(job.id if job else None), srt_path=c.srt_path,
    )


@router.post("/projects/{project_id}/export", status_code=202)
def create_export(project_id: str, payload: ExportCreate,
                  db: Session = Depends(get_db)) -> ParseJobOut:
    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可导出")
    comp = db.get(Composition, payload.composition_id)
    if comp is None or comp.project_id != project_id:
        raise errors.biz_error(code="composition_required", http=409,
                               message="不存在成功的合成版本，需先合成")
    if comp.stale:
        raise errors.biz_error(code="composition_stale", http=409,
                               message="合成版本已失效，需重新合成后再导出")
    if comp.status != "done":
        raise errors.biz_error(code="composition_required", http=409,
                               message="合成版本未完成")
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="export",
        params={
            "composition_id": comp.id,
            "delivery_profile": payload.delivery_profile,
        },
        status="queued", created_at=_now_naive(),
    )
    db.add(job)
    db.commit()
    return ParseJobOut(job_id=job.id, status=job.status)


@router.get("/projects/{project_id}/exports")
def list_exports(project_id: str, db: Session = Depends(get_db)) -> dict:
    _get_project(db, project_id)
    items = (
        db.query(Export)
        .filter(Export.project_id == project_id)
        .order_by(Export.created_at.desc())
        .all()
    )
    return {"total": len(items), "items": [{
        "id": e.id, "composition_id": e.composition_id, "file_name": e.file_name,
        "size": e.size, "status": e.status,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "download_url": f"/api/exports/{e.id}/download",
    } for e in items]}


@router.get("/projects/{project_id}/exports/{export_id}/acceptance-check")
def check_export_acceptance(project_id: str, export_id: str,
                            db: Session = Depends(get_db)) -> dict:
    """成片验收检查（阶段11）：对导出文件执行质量合规与完整性校验。

    - 使用 ffprobe 提取元数据，执行验收清单检查
    - 检查项：文件存在性、文件非空、视频流、音频轨、字幕轨、分辨率、时长、格式有效性
    - 返回结构化验收报告，不修改导出记录状态
    """
    from app.services.acceptance_check import run_acceptance_check
    from app.services.compose import resolution_for

    p = _get_project(db, project_id)
    e = db.get(Export, export_id)
    if e is None or e.project_id != project_id:
        raise errors.invalid_request("导出记录不存在")
    if not e.file_name:
        raise errors.invalid_request("导出文件名为空")
    file_path = Path(get_settings().media_dir) / e.file_name
    # 从合成版本获取期望时长
    comp = db.get(Composition, e.composition_id) if e.composition_id else None
    expected_duration = comp.duration if comp else None
    # 从项目宽高比推导期望分辨率
    expected_resolution = None
    if p.aspect_ratio:
        try:
            expected_resolution = resolution_for(p.aspect_ratio)
        except Exception:
            pass
    report = run_acceptance_check(
        export_id=export_id,
        file_path=str(file_path),
        file_name=e.file_name,
        expected_duration=expected_duration,
        expected_resolution=expected_resolution,
        aspect_ratio=p.aspect_ratio,
        require_audio=True,
        require_subtitle=False,
    )
    return report.to_dict()


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, db: Session = Depends(get_db)) -> FileResponse:
    e = db.get(Export, export_id)
    if e is None or not e.file_name:
        raise errors.invalid_request("导出文件不存在")
    file_path = Path(get_settings().media_dir) / e.file_name
    if not file_path.exists():
        raise errors.invalid_request("导出文件不存在")
    return FileResponse(
        path=str(file_path), filename=e.file_name, media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{e.file_name}"'},
    )
