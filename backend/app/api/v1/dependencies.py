"""项目生产依赖的只读聚合、前置追溯与影响分析接口。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Project
from app.services import errors
from app.services.dependency_graph import (
    DependencyNodeNotFound,
    build_dependency_graph,
    traverse_dependency_graph,
)
from app.services.dependency_impact import (
    preview_dependency_impact,
    preview_project_impact,
)

router = APIRouter(prefix="/api/projects", tags=["dependencies"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise errors.project_not_found()
    return project


@router.get("/{project_id}/dependency-graph")
def get_dependency_graph(project_id: str, db: Session = Depends(get_db)) -> dict:
    _get_project(db, project_id)
    return build_dependency_graph(db, project_id)


def _traverse(project_id: str, node_type: str, node_id: str, direction: str,
              db: Session) -> dict:
    _get_project(db, project_id)
    try:
        return traverse_dependency_graph(
            db, project_id, node_type=node_type, node_id=node_id, direction=direction,
        )
    except DependencyNodeNotFound:
        raise errors.AppError(
            "dependency_node_not_found", "依赖节点不存在或不属于本项目", 404,
        ) from None


@router.get("/{project_id}/dependency-graph/{node_type}/{node_id}/prerequisites")
def get_dependency_prerequisites(project_id: str, node_type: str, node_id: str,
                                 db: Session = Depends(get_db)) -> dict:
    return _traverse(project_id, node_type, node_id, "prerequisites", db)


@router.get("/{project_id}/dependency-graph/{node_type}/{node_id}/impact")
def get_dependency_impact(project_id: str, node_type: str, node_id: str,
                          db: Session = Depends(get_db)) -> dict:
    return _traverse(project_id, node_type, node_id, "impact", db)


@router.get("/{project_id}/dependency-graph/impact-preview")
def get_project_impact_preview(project_id: str, db: Session = Depends(get_db)) -> dict:
    """项目级只读总览：汇总所有因上游版本/失效变化而待重做、待重绑的对象。"""
    _get_project(db, project_id)
    return preview_project_impact(db, project_id)


@router.get("/{project_id}/dependency-graph/{node_type}/{node_id}/impact-preview")
def get_dependency_impact_preview(project_id: str, node_type: str, node_id: str,
                                  db: Session = Depends(get_db)) -> dict:
    _get_project(db, project_id)
    try:
        return preview_dependency_impact(
            db, project_id, node_type=node_type, node_id=node_id,
        )
    except DependencyNodeNotFound:
        raise errors.AppError(
            "dependency_node_not_found", "依赖节点不存在或不属于本项目", 404,
        ) from None
