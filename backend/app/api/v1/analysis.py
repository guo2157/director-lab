"""剧本结构分析的只读接口：Shadow Run 对比与外部分析文档校验。

- 不写库、不改剧本、不触发解析或生成。
- Shadow Run 与现有 Scene/Shot/Character/Prop 并行比较，只报告差异。
- 分析文档校验按原文区块证据逐项返回问题，HTTP 200 + valid=false。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Project, ScriptVersion
from app.schemas.script import AnalysisValidate
from app.services import errors
from app.services.script_analysis import shadow_run, validate_analysis
from app.services.source_index import build_source_index

router = APIRouter(prefix="/api/projects", tags=["script-analysis"])


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


@router.get("/{project_id}/script-versions/{script_version_id}/analysis/shadow-run")
def get_shadow_run(project_id: str, script_version_id: str,
                   db: Session = Depends(get_db)) -> dict:
    _get_project(db, project_id)
    script_version = _get_owned_script_version(db, project_id, script_version_id)
    return shadow_run(db, script_version, project_id=project_id)


@router.post("/{project_id}/script-versions/{script_version_id}/analysis/validate")
def validate_analysis_document(
    project_id: str, script_version_id: str, payload: AnalysisValidate,
    db: Session = Depends(get_db),
) -> dict:
    _get_project(db, project_id)
    script_version = _get_owned_script_version(db, project_id, script_version_id)
    index = build_source_index(
        script_version.content_snapshot,
        script_version_id=script_version.id,
        version=script_version.version,
    )
    return validate_analysis(payload.analysis, index)
