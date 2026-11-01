"""生成进度接口：把「正在生成什么 / 批量进度 / 是否全部生成完」聚合到一次查询。

API.md §生成进度：
- GET /api/projects/{id}/asset-generation-progress
  公共资产图（角色定妆图 / 场景基准图 / 道具锚定图）的总体进度与在途明细。
- GET /api/projects/{id}/keyframe-generation-progress
  分镜图（逐镜 keyframe）的总体进度与在途明细。

两个接口都返回同一套口径，供前端轮询：
- total / done / percent：总体进度（done 按「当前有效产物」计算，不依赖任务历史）
- inflight_*：在途任务数（queued / running / submission_unknown）与逐条明细
- failed_recent_*：最近失败任务数（默认近 1 小时），便于在前端提示重试
- has_inflight：为 False 即表示这一批次已全部结束（成功或失败），前端据此给出「全部生成完成」
- last_completed_at：最近一次出图完成时间

在途明细带 kind（character / scene / prop / keyframe）与实体名称，前端可直接展示
「正在生成：角色·郭嘉（42%）」这种可读状态，而不是只看到一个数字。
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Asset, Character, Job, Project, Prop, Scene, Shot
from app.services import errors
from app.services.progress import latest_progress_percents

router = APIRouter(prefix="/api/projects", tags=["progress"])

# 「在途」口径：与 worker 调度一致，终态以外的可等待状态都算在途
_INFLIGHT_STATUS = ("queued", "running", "submission_unknown")
# 资产图任务统一走 generate_keyframe，靠 params 里的 __xxx_id__ 区分资产/分镜
_ASSET_PARAM_KIND = (
    ("__character_id__", "character"),
    ("__scene_id__", "scene"),
    ("__prop_id__", "prop"),
)

_GENERATE_FAILED_STATUS = ("failed", "lost")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def _asset_job_target(job: Job) -> tuple[str, str]:
    """资产图任务 → (kind, entity_id)；分镜图任务（无 ____id__ 参数）返回 ('', '')。"""
    params = job.params or {}
    for key, kind in _ASSET_PARAM_KIND:
        value = params.get(key)
        if value:
            return kind, str(value)
    return "", ""


def _recent_failed_jobs(db: Session, project_id: str, job_type: str, *,
                        kinds: tuple[str, ...] | None, shot_level: bool,
                        failed_since: datetime) -> list[Job]:
    """最近失败的生成任务；资产类任务与分镜任务按 params 区分。"""
    rows = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == job_type,
            Job.status.in_(_GENERATE_FAILED_STATUS),
            Job.finished_at.isnot(None),
            Job.finished_at >= failed_since,
        )
        .order_by(Job.finished_at.desc())
        .all()
    )
    out: list[Job] = []
    for job in rows:
        kind, _entity_id = _asset_job_target(job)
        if shot_level:
            if not job.shot_id:
                continue
        elif kind not in (kinds or ()):
            continue
        out.append(job)
    return out


@router.get("/{project_id}/asset-generation-progress")
def asset_generation_progress(
    project_id: str,
    failed_hours: int = Query(1, ge=0, le=168, description="最近失败统计窗口（小时）"),
    db: Session = Depends(get_db),
) -> dict:
    """公共资产图进度：角色定妆图 / 场景基准图 / 道具锚定图。"""
    project = db.get(Project, project_id)
    if project is None:
        raise errors.project_not_found()

    chars = db.query(Character.id, Character.name, Character.portrait_asset_id).filter(
        Character.project_id == project_id
    ).all()
    scenes = db.query(Scene.id, Scene.title, Scene.baseline_asset_id).filter(
        Scene.project_id == project_id
    ).all()
    props = db.query(Prop.id, Prop.name, Prop.anchor_asset_id, Prop.anchor_required).filter(
        Prop.project_id == project_id
    ).all()

    required_props = [p for p in props if p.anchor_required]
    groups = {
        "character": {
            "label": "角色定妆图",
            "total": len(chars),
            "done": sum(1 for c in chars if c.portrait_asset_id),
        },
        "scene": {
            "label": "场景基准图",
            "total": len(scenes),
            "done": sum(1 for s in scenes if s.baseline_asset_id),
        },
        "prop": {
            "label": "道具锚定图",
            "total": len(required_props),
            "done": sum(1 for p in required_props if p.anchor_asset_id),
        },
    }
    total = sum(g["total"] for g in groups.values())
    done = sum(g["done"] for g in groups.values())

    names: dict[tuple[str, str], str] = {}
    names.update({("character", c.id): c.name for c in chars})
    names.update({("scene", s.id): s.title for s in scenes})
    names.update({("prop", p.id): p.name for p in props})

    inflight_jobs: list[Job] = []
    for job in (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_keyframe",
            Job.status.in_(_INFLIGHT_STATUS),
        )
        .order_by(Job.created_at.asc())
        .all()
    ):
        if _asset_job_target(job)[0]:
            inflight_jobs.append(job)

    progress_map = latest_progress_percents(
        db, [j.id for j in inflight_jobs if j.status == "running"]
    )

    inflight = []
    for job in inflight_jobs:
        kind, entity_id = _asset_job_target(job)
        inflight.append(
            {
                "job_id": job.id,
                "kind": kind,
                "kind_label": groups.get(kind, {}).get("label", kind),
                "entity_id": entity_id,
                "entity_name": names.get((kind, entity_id)) or entity_id,
                "status": job.status,
                "progress": progress_map.get(job.id),
                "error_msg": job.error_msg,
                "created_at": _iso(job.created_at),
                "started_at": _iso(job.started_at),
            }
        )

    last_done_job = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_keyframe",
            Job.status == "succeeded",
            Job.finished_at.isnot(None),
        )
        .order_by(Job.finished_at.desc())
        .first()
    )
    failed_since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=failed_hours)
    failed_jobs = _recent_failed_jobs(
        db,
        project_id,
        "generate_keyframe",
        kinds=tuple(groups.keys()),
        shot_level=False,
        failed_since=failed_since,
    )
    failed = [
        {
            "job_id": job.id,
            "kind": _asset_job_target(job)[0],
            "entity_id": _asset_job_target(job)[1],
            "entity_name": names.get(
                (_asset_job_target(job)[0], _asset_job_target(job)[1])
            ) or _asset_job_target(job)[1],
            "error_msg": job.error_msg,
            "finished_at": _iso(job.finished_at),
        }
        for job in failed_jobs
    ]

    return {
        "project_id": project_id,
        "total": total,
        "done": done,
        "percent": (round(done / total * 100) if total else 0),
        "all_done": total > 0 and done == total,
        "started": done > 0 or bool(inflight_jobs),
        "groups": groups,
        "inflight_count": len(inflight),
        "inflight_queued": sum(1 for j in inflight if j["status"] == "queued"),
        "inflight_running": sum(1 for j in inflight if j["status"] == "running"),
        "inflight": inflight,
        "failed_recent_count": len(failed),
        "failed_recent": failed[:10],
        "has_inflight": bool(inflight),
        "last_completed_at": _iso(last_done_job.finished_at if last_done_job else None),
        "assets_review_status": project.assets_review_status,
    }


@router.get("/{project_id}/keyframe-generation-progress")
def keyframe_generation_progress(
    project_id: str,
    failed_hours: int = Query(1, ge=0, le=168, description="最近失败统计窗口（小时）"),
    db: Session = Depends(get_db),
) -> dict:
    """分镜图进度：逐镜 keyframe（镜头级）。"""
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()

    rows = (
        db.query(Shot.id, Shot.seq, Shot.episode, Shot.keyframe_confirmed,
                 Scene.id, Scene.title, Scene.seq)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .order_by(Shot.episode, Scene.seq, Shot.seq)
        .all()
    )
    scene_ids = sorted({row[5] for row in rows})

    # 当前有效分镜图：一次性取回后回填，避免逐镜查询
    current_shots: set[str] = set()
    if scene_ids:
        current_shots = {
            shot_id
            for (shot_id,) in db.query(Job.shot_id)
            .join(Asset, Asset.source_job_id == Job.id)
            .filter(
                Job.shot_id.isnot(None),
                Job.job_type == "generate_keyframe",
                Job.status == "succeeded",
                Asset.asset_type == "keyframe",
                Asset.validity == "current",
                Job.shot_id.in_(
                    db.query(Shot.id).filter(Shot.scene_id.in_(scene_ids))
                ),
            )
            .distinct()
            .all()
            if shot_id
        }

    total = len(rows)
    with_keyframe = sum(1 for row in rows if row[0] in current_shots)
    confirmed = sum(1 for row in rows if row[0] in current_shots and row[3])

    shot_meta = {
        row[0]: {
            "shot_id": row[0],
            "seq": row[1],
            "episode": row[2],
            "scene_id": row[4],
            "scene_title": row[5],
            "scene_seq": row[6],
            "has_keyframe": row[0] in current_shots,
        }
        for row in rows
    }

    inflight_jobs = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_keyframe",
            Job.status.in_(_INFLIGHT_STATUS),
            Job.shot_id.isnot(None),
        )
        .order_by(Job.created_at.asc())
        .all()
    )
    progress_map = latest_progress_percents(
        db, [j.id for j in inflight_jobs if j.status == "running"]
    )

    inflight = []
    for job in inflight_jobs:
        meta = shot_meta.get(job.shot_id or "", {})
        inflight.append(
            {
                "job_id": job.id,
                "shot_id": job.shot_id,
                "shot_seq": meta.get("seq"),
                "episode": meta.get("episode"),
                "scene_id": meta.get("scene_id"),
                "scene_title": meta.get("scene_title"),
                "status": job.status,
                "progress": progress_map.get(job.id),
                "error_msg": job.error_msg,
                "created_at": _iso(job.created_at),
                "started_at": _iso(job.started_at),
            }
        )

    last_done_job = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_keyframe",
            Job.status == "succeeded",
            Job.shot_id.isnot(None),
            Job.finished_at.isnot(None),
        )
        .order_by(Job.finished_at.desc())
        .first()
    )
    failed_since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=failed_hours)
    failed_jobs = _recent_failed_jobs(
        db,
        project_id,
        "generate_keyframe",
        kinds=None,
        shot_level=True,
        failed_since=failed_since,
    )
    failed = [
        {
            "job_id": job.id,
            "shot_id": job.shot_id,
            "shot_seq": shot_meta.get(job.shot_id or "", {}).get("seq"),
            "scene_title": shot_meta.get(job.shot_id or "", {}).get("scene_title"),
            "error_msg": job.error_msg,
            "finished_at": _iso(job.finished_at),
        }
        for job in failed_jobs
    ]

    return {
        "project_id": project_id,
        "total": total,
        "with_keyframe": with_keyframe,
        "done": with_keyframe,
        "confirmed": confirmed,
        "percent": (round(with_keyframe / total * 100) if total else 0),
        "all_done": total > 0 and with_keyframe == total,
        "all_confirmed": total > 0 and confirmed == total,
        "started": with_keyframe > 0 or bool(inflight_jobs),
        "inflight_count": len(inflight),
        "inflight_queued": sum(1 for j in inflight if j["status"] == "queued"),
        "inflight_running": sum(1 for j in inflight if j["status"] == "running"),
        "inflight": inflight,
        "failed_recent_count": len(failed),
        "failed_recent": failed[:10],
        "has_inflight": bool(inflight),
        "last_completed_at": _iso(last_done_job.finished_at if last_done_job else None),
    }
