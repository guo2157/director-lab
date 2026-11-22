"""工作流版本影响预检（Phase 3 / 6.5）：只读比对历史任务的工作流版本与当前版本。

设计约定：
- 只读：不写库、不标记失效、不删除产物、不触发重生成。
- 只检查「已成功且有产出记录」的任务：失败/排队任务没有可影响的产物。
- 任务执行时的工作流版本来自 ``job.workflow_version``（历史任务语义固定），
  与 ``workflow.version``（当前版本）比较；工作流已停用或记录缺失单独分类。
- 判定基于任务实际使用的版本，不做「全项目一律失效」。
"""
from sqlalchemy.orm import Session

from app.models import Asset, Job, Scene, Shot, Workflow

WORKFLOW_IMPACT_ALGORITHM = "workflow-impact-v1"

_READ_ONLY_POLICY = {
    "auto_regenerate": False,
    "mark_stale": False,
    "delete_existing": False,
    "writes": False,
    "note": "仅比对工作流版本；是否重做由用户确认后单独触发",
}


def _classify(job_version: int | None, current_version: int | None,
              workflow_status: str | None) -> str:
    """版本一致且已发布 → 已最新；否则按停用/版本变化/未知分类。"""
    if current_version is None:
        return "unknown_workflow_version"
    if workflow_status != "published":
        return "workflow_disabled"
    if job_version != current_version:
        return "workflow_changed"
    return "up_to_date"


def _output_assets(db: Session, job_ids: list[str]) -> dict[str, Asset]:
    """每个任务取最新版本产出资产（旧版本保留但以最新版代表当前产物）。"""
    if not job_ids:
        return {}
    rows = (
        db.query(Asset)
        .filter(Asset.source_job_id.in_(job_ids))
        .order_by(Asset.version.desc())
        .all()
    )
    latest: dict[str, Asset] = {}
    for asset in rows:
        latest.setdefault(asset.source_job_id, asset)
    return latest


def preview_workflow_impact(db: Session, project_id: str) -> dict:
    """列出因工作流版本变化/停用而可能受影响的已生成任务；不写库。"""
    rows = (
        db.query(Job, Workflow)
        .outerjoin(Workflow, Workflow.id == Job.workflow_id)
        .filter(
            Job.project_id == project_id,
            Job.workflow_id.isnot(None),
            Job.workflow_version.isnot(None),
            Job.status == "succeeded",
        )
        .all()
    )
    if not rows:
        return {
            "algorithm": WORKFLOW_IMPACT_ALGORITHM,
            "policy": dict(_READ_ONLY_POLICY),
            "items": [],
            "summary": {
                "checked_job_count": 0,
                "affected_job_count": 0,
                "affected_shot_count": 0,
                "workflow_changed": 0,
                "workflow_disabled": 0,
                "unknown_workflow_version": 0,
            },
        }

    shot_ids = {job.shot_id for job, _ in rows if job.shot_id is not None}
    shots = {
        shot.id: shot
        for shot in db.query(Shot).filter(Shot.id.in_(shot_ids)).all()
    } if shot_ids else {}
    scene_ids = {shot.scene_id for shot in shots.values()}
    scenes = {
        scene.id: scene
        for scene in db.query(Scene).filter(Scene.id.in_(scene_ids)).all()
    } if scene_ids else {}

    output_by_job = _output_assets(db, [job.id for job, _ in rows])

    items: list[dict] = []
    for job, workflow in rows:
        current_version = workflow.version if workflow is not None else None
        workflow_status = workflow.status if workflow is not None else None
        change_status = _classify(job.workflow_version, current_version, workflow_status)
        if change_status == "up_to_date":
            continue
        shot = shots.get(job.shot_id) if job.shot_id is not None else None
        scene = scenes.get(shot.scene_id) if shot is not None else None
        output = output_by_job.get(job.id)
        items.append({
            "job_id": job.id,
            "job_type": job.job_type,
            "shot_id": job.shot_id,
            "scene_id": shot.scene_id if shot is not None else None,
            "scene_title": scene.title if scene is not None else None,
            "episode": shot.episode if shot is not None else None,
            "seq": shot.seq if shot is not None else None,
            "workflow_id": job.workflow_id,
            "workflow_name": workflow.name if workflow is not None else None,
            "job_workflow_version": job.workflow_version,
            "current_workflow_version": current_version,
            "workflow_status": workflow_status,
            "change_status": change_status,
            "has_output": output is not None,
            "output_asset": None if output is None else {
                "asset_id": output.id,
                "asset_type": output.asset_type,
                "name": output.name,
                "version": output.version,
                "validity": output.validity,
            },
        })

    items.sort(key=lambda item: (
        item["episode"] or 0, item["seq"] or 0, item["job_id"],
    ))
    return {
        "algorithm": WORKFLOW_IMPACT_ALGORITHM,
        "policy": dict(_READ_ONLY_POLICY),
        "items": items,
        "summary": {
            "checked_job_count": len(rows),
            "affected_job_count": len(items),
            "affected_shot_count": len({item["shot_id"] for item in items if item["shot_id"]}),
            "workflow_changed": sum(item["change_status"] == "workflow_changed" for item in items),
            "workflow_disabled": sum(item["change_status"] == "workflow_disabled" for item in items),
            "unknown_workflow_version": sum(
                item["change_status"] == "unknown_workflow_version" for item in items
            ),
        },
    }
