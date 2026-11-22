"""分镜规划接口：校验/预览为只读，apply 为显式写入。

- validate：只做契约校验、Shot 兼容 Adapter 预览与既有镜头对比，不写库。
- apply：仅在规划零错误时安全 upsert Shot；不覆盖锁定镜头的关键字段、
  不删除规划外的旧镜头、不重建资产计划、不触发生成。
- Prompt 编译仍由生产层负责。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Project, ScriptVersion
from app.schemas.storyboard import StoryboardPlanValidate
from app.services import errors
from app.services.storyboard_plan import apply_storyboard_plan, validate_storyboard_plan

router = APIRouter(prefix="/api/projects", tags=["storyboard-planning"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise errors.project_not_found()
    return project


def _get_owned_script_version(db: Session, project_id: str, sv_id: str) -> ScriptVersion:
    script_version = db.get(ScriptVersion, sv_id)
    if script_version is None or script_version.project_id != project_id:
        raise errors.script_version_not_found()
    return script_version


@router.post("/{project_id}/script-versions/{script_version_id}/storyboard-plan/validate")
def validate_plan(project_id: str, script_version_id: str,
                  payload: StoryboardPlanValidate,
                  db: Session = Depends(get_db)) -> dict:
    _get_project(db, project_id)
    script_version = _get_owned_script_version(db, project_id, script_version_id)
    return validate_storyboard_plan(
        db, script_version, project_id=project_id, plan=payload.plan,
    )


@router.post("/{project_id}/script-versions/{script_version_id}/storyboard-plan/apply")
def apply_plan(project_id: str, script_version_id: str,
               payload: StoryboardPlanValidate,
               db: Session = Depends(get_db)) -> dict:
    """显式写入规划：安全 upsert，锁定镜头不覆盖，不删除旧镜头，不触发生成。"""
    _get_project(db, project_id)
    script_version = _get_owned_script_version(db, project_id, script_version_id)
    result = apply_storyboard_plan(
        db, script_version, project_id=project_id, plan=payload.plan,
    )
    if result.get("applied"):
        return result

    reason = result.get("reason")
    if reason == "validation_failed":
        error_count = sum(1 for issue in result["issues"] if issue["level"] == "error")
        raise errors.biz_error(
            code="storyboard_plan_invalid", http=400,
            message=f"规划校验未通过：{error_count} 个错误，未写入",
            issues=result["issues"],
        )
    if reason == "missing_plan_version":
        raise errors.biz_error(
            code="storyboard_plan_invalid", http=400,
            message="plan_version 必须为正整数，未写入",
            issues=result["issues"],
        )
    if reason == "sequence_conflict":
        raise errors.biz_error(
            code="storyboard_plan_conflict", http=409,
            message="规划序号与未纳入本次写入的既有镜头冲突，未写入",
            conflicts=result.get("conflicts", []),
        )
    if reason == "shot_id_conflict":
        raise errors.biz_error(
            code="storyboard_plan_conflict", http=409,
            message="规划镜头 ID 已被其他项目占用，未写入",
            problems=result.get("problems", []),
        )
    raise errors.biz_error(
        code="storyboard_plan_invalid", http=400,
        message="规划无法写入，未做任何修改",
        problems=result.get("problems", []),
    )
