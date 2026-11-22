"""分镜规划只读接口：规划文档校验、Shot 兼容 Adapter 预览与既有镜头对比。

不写 Shot、不触发生成、不覆盖锁定镜头；Prompt 编译仍由生产层负责。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Project, ScriptVersion
from app.schemas.storyboard import StoryboardPlanValidate
from app.services import errors
from app.services.storyboard_plan import validate_storyboard_plan

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
