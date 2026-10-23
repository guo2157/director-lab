"""生产进度派生：ARCHITECTURE.md §4.1 current_stage 推导规则（按顺序取第一个满足条件的值）。

产物具备判定（§4.2）：存在指定 asset_type、validity=current 且来源任务 succeeded 的产物；
不使用 asset_ref 单独判定，须结合资产有效性与来源任务状态。
"""
from sqlalchemy.orm import Session

from app.models import Asset, AssetRef, Character, Job, Project, Scene, ScriptVersion, Shot


def _shot_has_current_asset(db: Session, shot_id: str, asset_type: str) -> bool:
    """镜头是否具备指定类型的当前有效产物（validity=current 且来源任务 succeeded）。"""
    rows = (
        db.query(Asset)
        .join(AssetRef, AssetRef.asset_id == Asset.id)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            AssetRef.shot_id == shot_id,
            Asset.asset_type == asset_type,
            Asset.validity == "current",
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

    # 规则 1：未评估或剧本已变更
    if not project.estimated_text_hash or not project.current_script_version_id:
        return shot_total, shot_done, "estimate"
    sv = db.get(ScriptVersion, project.current_script_version_id)
    if sv is None or sv.content_hash != project.estimated_text_hash:
        return shot_total, shot_done, "estimate"

    # 规则 2：已评估但不存在解析任务
    parse_jobs = db.query(Job).filter(Job.project_id == project.id, Job.job_type == "parse").count()
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

    # 规则 6：存在分镜图未完成的镜头
    for s in shots:
        if not _shot_has_current_asset(db, s.id, "keyframe"):
            return shot_total, shot_done, "keyframes"

    # 规则 7：分镜图全部完成，但存在视频未完成的镜头
    for s in shots:
        if not _shot_has_current_asset(db, s.id, "clip"):
            return shot_total, shot_done, "clips"

    # 规则 8/9：音频检查与音画容差（音频中间态按不具备处理）
    for s in shots:
        if s.shot_kind in ("dialogue", "narration"):
            audio_ok = _shot_has_current_asset(db, s.id, "audio")
            if not audio_ok:
                return shot_total, shot_done, "audio"
            if s.av_sync_delta is not None and s.status == "review":
                return shot_total, shot_done, "audio"

    # 规则 10-12：合成与导出（阶段 6 完整落地，此处按表记录判定）
    from app.models import Composition, Export

    comp = (
        db.query(Composition)
        .filter(
            Composition.project_id == project.id,
            Composition.status == "succeeded",
            Composition.stale.is_(False),
        )
        .order_by(Composition.created_at.desc())
        .first()
    )
    if comp is None:
        return shot_total, shot_done, "compose"
    exp = (
        db.query(Export)
        .filter(Export.composition_id == comp.id, Export.status == "succeeded")
        .first()
    )
    if exp is None:
        return shot_total, shot_done, "export"
    return shot_total, shot_done, "done"
