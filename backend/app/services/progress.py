"""生产进度派生：ARCHITECTURE.md §4.1 current_stage 推导规则（按顺序取第一个满足条件的值）。

产物具备判定（§4.2）：存在指定 asset_type 且来源任务 succeeded 的产物；
不使用 asset_ref 单独判定，须结合资产与来源任务状态。
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Asset, AssetRef, Character, Job, JobEvent, Project, Scene, ScriptVersion, Shot


def job_progress_percent(payload: dict | None) -> int | None:
    """job.progress 事件 payload（value/max）换算 0-100；缺值或非法时返回 None。"""
    if not payload:
        return None
    value = payload.get("value")
    maxv = payload.get("max")
    if not isinstance(value, (int, float)) or not isinstance(maxv, (int, float)) or maxv <= 0:
        return None
    return max(0, min(100, round(value / maxv * 100)))


def latest_progress_percents(db: Session, job_ids: list[str]) -> dict[str, int]:
    """批量取多个任务的最新 job.progress 百分比（一次聚合查询，避免逐任务查询）。

    仅用于 running 任务：同一任务可能有多次 progress 事件，取 event_seq 最大的一条。
    """
    if not job_ids:
        return {}
    latest_seq = (
        db.query(JobEvent.job_id.label("job_id"), func.max(JobEvent.event_seq).label("max_seq"))
        .filter(JobEvent.job_id.in_(job_ids), JobEvent.event_type == "job.progress")
        .group_by(JobEvent.job_id)
        .subquery()
    )
    rows = (
        db.query(JobEvent.job_id, JobEvent.payload)
        .join(latest_seq, JobEvent.event_seq == latest_seq.c.max_seq)
        .all()
    )
    out: dict[str, int] = {}
    for job_id, payload in rows:
        pct = job_progress_percent(payload)
        if pct is not None:
            out[job_id] = pct
    return out


def _shot_has_current_asset(db: Session, shot_id: str, asset_type: str) -> bool:
    """镜头是否具备指定类型的产物（来源任务 succeeded）。"""
    rows = (
        db.query(Asset)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id == shot_id,
            Asset.asset_type == asset_type,
            Job.status == "succeeded",
        )
        .count()
    )
    return rows > 0


def derive_progress(db: Session, project: Project) -> tuple[int, int, str]:
    """返回 (shot_total, shot_done, current_stage)。"""
    shots = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project.id)
        .all()
    )
    shot_total = len(shots)
    shot_done = sum(1 for s in shots if s.status == "done")

    # 规则 1：未评估或剧本已变更 → 需要重新评估
    # 例外：已经解析出镜头的项目视为剧本已可用（剧本一体化分析不一定经过文字评估这一步，
    # 若只看 estimated_text_hash 会把「已有分镜/已出视频」的项目永远判在 estimate，
    # 导致 Agent 对项目进度给出「当前项目还没有可用的剧本」这种明显错误的结论）
    if shot_total == 0 and (
        not project.estimated_text_hash or not project.current_script_version_id
    ):
        return shot_total, shot_done, "estimate"
    if shot_total == 0 and project.current_script_version_id:
        sv = db.get(ScriptVersion, project.current_script_version_id)
        if sv is None or sv.content_hash != project.estimated_text_hash:
            return shot_total, shot_done, "estimate"

    # 规则 2：已评估但不存在解析任务（script_agent 与 parse 均计入）
    parse_jobs = db.query(Job).filter(
        Job.project_id == project.id,
        Job.job_type.in_(["parse", "script_agent"]),
    ).count()
    if parse_jobs == 0:
        return shot_total, shot_done, "select_range"

    # 规则 3：存在解析任务但镜头数为 0
    if shot_total == 0:
        return shot_total, shot_done, "parse"

    # 规则 4：存在角色定妆图为空（项目级判定）
    missing_portrait = (
        db.query(Character)
        .filter(Character.project_id == project.id, Character.portrait_asset_id.is_(None))
        .count()
    )
    if missing_portrait > 0:
        return shot_total, shot_done, "characters"

    # 规则 5：全部角色具备定妆图，但存在场景基准图为空（项目级判定）
    missing_baseline = (
        db.query(Scene)
        .filter(Scene.project_id == project.id, Scene.baseline_asset_id.is_(None))
        .count()
    )
    if missing_baseline > 0:
        return shot_total, shot_done, "scenes"

    # 规则 5.5：公共资产已齐全，但尚未通过人工审核（D-23 资产审核闸口）
    # 资产库出图后必须人工确认，未通过前不得进入分镜图生成
    if (project.assets_review_status or "pending") != "approved":
        return shot_total, shot_done, "assets_review"
    # 规则 6：存在分镜图未完成或未人工确认的镜头（D-13 静图先行闸口）
    for s in shots:
        if not _shot_has_current_asset(db, s.id, "keyframe") or not s.keyframe_confirmed:
            return shot_total, shot_done, "keyframes"

    # 规则 7：分镜图全部完成，但存在视频未完成的镜头
    for s in shots:
        if not _shot_has_current_asset(db, s.id, "clip"):
            return shot_total, shot_done, "clips"

    # 规则 8/9：音频检查与音画容差（音频中间态按不具备处理）
    # H3 VA 的音轨内嵌在 clip 中、不产生 audio 资产，必须复用镜头完成度口径判定
    from app.services.shot_state import shot_audio_ready

    for s in shots:
        if s.shot_kind in ("dialogue", "narration"):
            if not shot_audio_ready(db, s, has_clip=True):
                return shot_total, shot_done, "audio"
            if s.av_sync_delta is not None and s.status == "review":
                return shot_total, shot_done, "audio"

    # 规则 10-12：合成与导出（阶段 6 完整落地，此处按表记录判定）
    from app.models import Composition, Export

    comp = (
        db.query(Composition)
        .filter(
            Composition.project_id == project.id,
            Composition.status.in_(("done", "succeeded")),
            Composition.stale.is_(False),
        )
        .order_by(Composition.created_at.desc())
        .first()
    )
    if comp is None:
        return shot_total, shot_done, "compose"
    exp = (
        db.query(Export)
        .filter(Export.composition_id == comp.id, Export.status.in_(("done", "succeeded")))
        .first()
    )
    if exp is None:
        return shot_total, shot_done, "export"
    return shot_total, shot_done, "done"
