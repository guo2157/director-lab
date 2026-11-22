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
# 分镜视频额外有 chained_wait（链式批量：前一镜成功后才会释放），同样属于在途
_CLIP_INFLIGHT_STATUS = ("queued", "running", "submission_unknown", "chained_wait")
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


def _not_superseded_by_success(
    db: Session,
    project_id: str,
    job_type: str,
    failed_jobs: list[Job],
    key_of,
) -> list[Job]:
    """剔除「之后又重新生成成功」的失败记录。

    事故（2026-11-21，用户实测「狼来了」项目）：镜 3 / 镜 4 分镜图先失败
    （请求超时），随后重新生成成功、图已正常显示，但卡片下方仍挂着
    「失败：请求超时: The write operation timed out」的红条。

    根因：failed_recent 只按时间窗口取失败任务，完全不看同目标之后是否已成功，
    前端再按镜头去重展示 → 成功重画也无法消掉旧失败。分镜视频进度同构，
    资产图（角色/场景/道具）重画成功后也会留同样的幽灵红条。

    判据：同一目标存在 finished_at 更晚的 succeeded 任务 → 这条失败已过期。
    「失败 → 成功 → 再失败」时最后那条失败时间晚于成功，会保留（是真失败）。

    key_of(job) 返回目标键（shot_id / (kind, entity_id)），返回 None 表示
    无法定位目标，这类记录原样保留（宁可多提示，不可误吞真实失败）。
    """
    if not failed_jobs:
        return []

    latest_fail_at: dict = {}
    earliest_fail_at: datetime | None = None
    for job in failed_jobs:
        if job.finished_at is None:
            continue
        if earliest_fail_at is None or job.finished_at < earliest_fail_at:
            earliest_fail_at = job.finished_at
        key = key_of(job)
        if key is None:
            continue
        cur = latest_fail_at.get(key)
        if cur is None or job.finished_at > cur:
            latest_fail_at[key] = job.finished_at
    if not latest_fail_at or earliest_fail_at is None:
        return list(failed_jobs)

    # 从最早一条失败开始找成功：失败 → 成功 → 再失败 时，
    # 每个失败各自跟「同一目标的更晚成功」比较，不能只看最新那次失败的时间点。
    successes = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == job_type,
            Job.status == "succeeded",
            Job.finished_at.isnot(None),
            Job.finished_at >= earliest_fail_at,
        )
        .all()
    )
    ok_at: dict = {}
    for job in successes:
        key = key_of(job)
        if key is None or key not in latest_fail_at or job.finished_at is None:
            continue
        cur = ok_at.get(key)
        if cur is None or job.finished_at > cur:
            ok_at[key] = job.finished_at
    if not ok_at:
        return list(failed_jobs)

    out: list[Job] = []
    for job in failed_jobs:
        ok = ok_at.get(key_of(job))
        if ok is not None and job.finished_at is not None and job.finished_at < ok:
            continue
        out.append(job)
    return out


def _shot_key(job: Job) -> str | None:
    return job.shot_id or None


def _asset_key(job: Job) -> tuple[str, str] | None:
    kind, entity_id = _asset_job_target(job)
    return (kind, entity_id) if kind and entity_id else None


def generation_progress_snapshot(db: Session, project_id: str) -> dict:
    """生成进度快照（供 Agent 对话上下文复用，与 HTTP 接口同口径）。

    返回三块聚合结果，调用方只取需要的字段拼进提示词，不再重复实现统计逻辑。
    项目不存在时返回空字典（Agent 侧不需要再抛 404）。
    """
    if db.get(Project, project_id) is None:
        return {}
    return {
        "assets": asset_generation_progress(project_id, db=db),
        "keyframes": keyframe_generation_progress(project_id, db=db),
        "clips": clip_generation_progress(project_id, db=db),
    }


@router.get("/{project_id}/asset-generation-progress")
def asset_generation_progress(
    project_id: str,
    failed_hours: int = 1,
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

    # 道具与页面展示口径一致：制作资产区列出全部道具（不按 anchor_required 过滤），
    # 否则会出现「页面上 8/8 已生成、进度面板 0/0」的自相矛盾
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
            "total": len(props),
            "done": sum(1 for p in props if p.anchor_asset_id),
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
    # 实体重画成功后不再提示旧的失败（否则资产卡会一直挂红条）
    failed_jobs = _not_superseded_by_success(
        db, project_id, "generate_keyframe", failed_jobs, _asset_key
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
    failed_hours: int = 1,
    db: Session = Depends(get_db),
) -> dict:
    """分镜图进度：逐镜 keyframe（镜头级）。"""
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()

    # 逐镜人工确认（keyframe_confirmed）已取消：进度只反映产物是否生成，
    # 不再查询确认标记，也不返回 confirmed / all_confirmed 统计。
    rows = (
        db.query(Shot.id, Shot.seq, Shot.episode,
                 Scene.id, Scene.title, Scene.seq)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .order_by(Shot.episode, Scene.seq, Shot.seq)
        .all()
    )
    scene_ids = sorted({row[3] for row in rows})

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

    shot_meta = {
        row[0]: {
            "shot_id": row[0],
            "seq": row[1],
            "episode": row[2],
            "scene_id": row[3],
            "scene_title": row[4],
            "scene_seq": row[5],
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
    # 已重新生成成功的镜头不再提示旧失败（分镜图卡片幽灵红条）
    failed_jobs = _not_superseded_by_success(
        db, project_id, "generate_keyframe", failed_jobs, _shot_key
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
        "percent": (round(with_keyframe / total * 100) if total else 0),
        "all_done": total > 0 and with_keyframe == total,
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


@router.get("/{project_id}/stage-readiness/{stage}")
def stage_readiness(project_id: str, stage: str, db: Session = Depends(get_db)) -> dict:
    """统一阶段就绪检查：资产库 / 分镜图 / 分镜视频 / 合成导出。

    - assets：资产库 → 分镜图，校验必需角色/场景/必需道具图片是否真实生成完成
      （内部复用 assets_gate，与后端生成门禁同口径）；
    - storyboard_images / storyboard_videos / compose：镜头级产物就绪判定。

    前端点击阶段跳转按钮时先查本接口：未就绪则展示阻断镜头明细，
    已就绪才弹出确认窗口；确认后前端再查一次（防止弹窗期间状态变化）。

    与进度接口的区别：进度接口回答「做到哪了」（含在途与失败统计），
    本接口回答「能不能进入下一阶段」——判定口径是当前有效产物 + 文件真实存在，
    不看任务历史，也不看任何人工审核状态。
    """
    from app.services.stage_readiness import (
        STAGE_ASSETS,
        STAGE_COMPOSE,
        STAGE_STORYBOARD_IMAGES,
        STAGE_STORYBOARD_VIDEOS,
        check_stage_readiness,
    )

    allowed = (
        STAGE_ASSETS,
        STAGE_STORYBOARD_IMAGES,
        STAGE_STORYBOARD_VIDEOS,
        STAGE_COMPOSE,
    )
    if stage not in allowed:
        raise errors.AppError(
            "invalid_request",
            "stage 必须为 assets / storyboard_images / storyboard_videos / compose",
            400,
        )
    if db.get(Project, project_id) is None:
        raise errors.project_not_found()
    return check_stage_readiness(db, project_id, stage).to_dict()


@router.get("/{project_id}/clip-generation-progress")
def clip_generation_progress(
    project_id: str,
    failed_hours: int = 1,
    db: Session = Depends(get_db),
) -> dict:
    """分镜视频进度：逐镜 generate_clip（分镜看板）。

    链式批量（chain_mode=chained）时同一时刻只有一个任务真正在跑，其余是 chained_wait；
    因此额外返回 queue_rank（按 worker 取任务顺序排队名次）与 estimated_wait_seconds，
    让用户知道「还要等多久」——这是长队列下最需要的进度信息。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise errors.project_not_found()

    rows = (
        db.query(Shot.id, Shot.seq, Shot.episode, Scene.id, Scene.title, Scene.seq)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .order_by(Shot.episode, Scene.seq, Shot.seq)
        .all()
    )
    scene_ids = sorted({row[3] for row in rows})

    current_clips: set[str] = set()
    if scene_ids:
        current_clips = {
            shot_id
            for (shot_id,) in db.query(Job.shot_id)
            .join(Asset, Asset.source_job_id == Job.id)
            .filter(
                Job.shot_id.isnot(None),
                Job.job_type == "generate_clip",
                Job.status == "succeeded",
                Asset.asset_type == "clip",
                Asset.validity == "current",
                Job.shot_id.in_(db.query(Shot.id).filter(Shot.scene_id.in_(scene_ids))),
            )
            .distinct()
            .all()
            if shot_id
        }

    total = len(rows)
    with_clip = sum(1 for row in rows if row[0] in current_clips)
    shot_meta = {
        row[0]: {
            "shot_id": row[0],
            "seq": row[1],
            "episode": row[2],
            "scene_id": row[3],
            "scene_title": row[4],
            "has_clip": row[0] in current_clips,
        }
        for row in rows
    }

    # 与 worker._next_job 同序：priority(+重试加权) 降序，同优先级按入队时间
    inflight_jobs = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_clip",
            Job.status.in_(_CLIP_INFLIGHT_STATUS),
            Job.shot_id.isnot(None),
        )
        .order_by(Job.priority.desc(), Job.created_at.asc())
        .all()
    )
    progress_map = latest_progress_percents(
        db, [j.id for j in inflight_jobs if j.status == "running"]
    )

    # 平均单镜耗时（近 7 日成功视频任务）→ 估算整条队列的剩余时间
    from datetime import timedelta as _timedelta

    week_ago = datetime.now(timezone.utc).replace(tzinfo=None) - _timedelta(days=7)
    recent = (
        db.query(Job.started_at, Job.finished_at)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_clip",
            Job.status == "succeeded",
            Job.started_at.isnot(None),
            Job.finished_at.isnot(None),
            Job.finished_at >= week_ago,
        )
        .all()
    )
    durations = [
        (finished - started).total_seconds()
        for started, finished in recent
        if started and finished and finished > started
    ]
    avg_seconds = round(sum(durations) / len(durations)) if durations else None

    inflight = []
    queue_ahead = 0
    for job in inflight_jobs:
        meta = shot_meta.get(job.shot_id or "", {})
        # 排队名次：running 视为正在执行的 0 号，其余按顺序计前面还有几个未完成
        rank = 0 if job.status == "running" else queue_ahead + 1
        if job.status != "running":
            queue_ahead += 1
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
                "queue_rank": rank,
                "error_msg": job.error_msg,
                "created_at": _iso(job.created_at),
                "started_at": _iso(job.started_at),
            }
        )

    # 估算剩余时间：正在跑的按剩余百分比折算，排队的按平均耗时整段计入
    estimated_wait_seconds: int | None = None
    if avg_seconds is not None and inflight:
        remaining = 0.0
        for job in inflight:
            if job["status"] == "running" and isinstance(job["progress"], int):
                remaining += avg_seconds * max(0.0, 1 - job["progress"] / 100)
            else:
                remaining += avg_seconds
        estimated_wait_seconds = round(remaining)

    last_done_job = (
        db.query(Job)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_clip",
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
        "generate_clip",
        kinds=None,
        shot_level=True,
        failed_since=failed_since,
    )
    # 分镜看板同理：视频重画成功后不再显示「视频生成失败」角标
    failed_jobs = _not_superseded_by_success(
        db, project_id, "generate_clip", failed_jobs, _shot_key
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
        "with_clip": with_clip,
        "done": with_clip,
        "percent": (round(with_clip / total * 100) if total else 0),
        "all_done": total > 0 and with_clip == total,
        "started": with_clip > 0 or bool(inflight_jobs),
        "inflight_count": len(inflight),
        "inflight_queued": sum(1 for j in inflight if j["status"] == "queued"),
        "inflight_running": sum(1 for j in inflight if j["status"] == "running"),
        "inflight_chained_wait": sum(1 for j in inflight if j["status"] == "chained_wait"),
        "inflight": inflight,
        "avg_seconds": avg_seconds,
        "estimated_wait_seconds": estimated_wait_seconds,
        "failed_recent_count": len(failed),
        "failed_recent": failed[:10],
        "has_inflight": bool(inflight),
        "last_completed_at": _iso(last_done_job.finished_at if last_done_job else None),
    }
