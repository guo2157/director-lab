"""统一任务查询接口（对话式异步任务整改）。

用途：
- 页面刷新 / 会话切换后，前端据 task_id 或 session_id 恢复任务真实状态
- 任务卡片按 task_payload 渲染：状态、进度、当前步骤、耗时、结果
- 耗时一律由后端 started_at 计算，前端不自行累加

状态取值：queued / running / completed / failed / cancelled（pending 为历史取值，等价 queued）。
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.task import Task
from app.services import task_orchestrator
from app.services.errors import biz_error

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _normalize_status(status: str | None) -> str | None:
    """历史 pending 与 queued 等价，统一对外暴露 queued。"""
    if status == "pending":
        return "queued"
    return status


def _payload(db: Session, task: Task) -> dict:
    data = task_orchestrator.task_payload(db, task)
    data["status"] = _normalize_status(data.get("status"))
    for stage in data.get("stages") or []:
        if stage.get("status") == "pending":
            stage["status"] = "queued"
    return data


@router.get("/{task_id}")
def get_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    """单任务详情：刷新或切换会话后按 task_id 恢复任务卡片。"""
    task = db.get(Task, task_id)
    if task is None:
        raise biz_error(code="task_not_found", http=404, message="任务不存在")
    return _payload(db, task)


@router.get("")
def list_tasks(
    session_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None, description="queued/running/completed/failed/cancelled"),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """任务列表：用于恢复会话内进行中/历史任务，或按项目巡检。"""
    q = db.query(Task)
    if session_id:
        q = q.filter(Task.session_id == session_id)
    if project_id:
        q = q.filter(Task.project_id == project_id)
    if status:
        if status == "queued":
            q = q.filter(Task.status.in_(["queued", "pending"]))
        else:
            q = q.filter(Task.status == status)
    tasks = q.order_by(Task.created_at.desc()).limit(limit).all()
    return {
        "total": len(tasks),
        "items": [_payload(db, t) for t in tasks],
    }


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, db: Session = Depends(get_db)) -> dict:
    """取消任务：仅未进入终态的任务可取消，重复取消返回当前状态。"""
    task = db.get(Task, task_id)
    if task is None:
        raise biz_error(code="task_not_found", http=404, message="任务不存在")
    if task.status in ("completed", "failed", "cancelled"):
        return {"ok": False, "status": _normalize_status(task.status), "message": "任务已处于终态，无需取消"}
    ok = task_orchestrator.cancel_task(db, task, reason="用户取消")
    db.commit()
    return {"ok": ok, "status": _normalize_status(task.status)}
