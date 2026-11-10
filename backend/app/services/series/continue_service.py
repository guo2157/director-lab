"""续写任务创建服务：只创建任务，不同步等待剧本生成（对应需求 §16.2）。

职责：
1. 确保剧集拥有独立生产工作空间 Project（没有则创建，不改动既有 Project）。
2. 创建统一 Task 与四个阶段（context_building / writing / validating / awaiting_review），
   阶段依赖由 depends_on 表达，进度全部由真实执行写入。
3. 创建 job_type=series_agent 的 Job，交由现有 worker 调度执行。

纪律：不在这里生成任何剧本内容，也不修改正式剧情状态。
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Job, Project, Series, SeriesEpisode
from app.models import ids
from app.services import task_orchestrator
from app.services import errors

from .episode_service import get_episode

STAGE_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("context_building", "构建续写上下文"),
    ("writing", "生成剧情大纲与剧本"),
    ("validating", "执行连续性检查"),
    ("awaiting_review", "等待剧本审核"),
)


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def ensure_project(db: Session, series: Series, episode: SeriesEpisode) -> Project:
    """确保剧集有独立生产工作空间；已存在则直接复用，绝不重建历史 Project。"""
    if episode.project_id:
        existing = db.get(Project, episode.project_id)
        if existing is not None and existing.deleted_at is None:
            return existing
    project = Project(
        id=ids.project_id(),
        name=f"{series.name} 第 {episode.episode_number} 集"[:200],
        creation_type="novel",
        description=episode.synopsis or series.description,
        aspect_ratio="16:9",
        status="draft",
        settings=None,
    )
    db.add(project)
    db.flush()
    episode.project_id = project.id
    episode.project_episode_no = 1
    episode.updated_at = _now()
    db.flush()
    return project


def create_continue_task(
    db: Session,
    *,
    series: Series,
    episode: SeriesEpisode,
    user_instruction: str,
    direction: str,
    constraints: dict | None,
    title: str | None = None,
) -> tuple[str, str]:
    """返回 (task_id, job_id)。任务创建后由 worker 异步执行。"""
    if series.status != "active":
        raise errors.invalid_state_transition("系列已归档，不能继续创作")
    if episode.script_status == "confirmed":
        raise errors.series_script_status_conflict("该集剧本已确认，如需修改请创建新的草稿版本")

    project = ensure_project(db, series, episode)

    task = task_orchestrator.create_task(
        db,
        session_id=None,
        task_type="series_continue",
        title=title or f"续写《{series.name}》第 {episode.episode_number} 集",
        project_id=project.id,
    )

    previous: str | None = None
    for stage_key, stage_name in STAGE_SEQUENCE:
        task_orchestrator.add_stage(
            db,
            task,
            stage_key=stage_key,
            stage_name=stage_name,
            depends_on=[previous] if previous else None,
            agent_name="episode_writing_agent",
        )
        previous = stage_key

    job = Job(
        id=ids.job_id(),
        project_id=project.id,
        shot_id=None,
        job_type="series_agent",
        params={
            "series_id": series.id,
            "episode_id": episode.id,
            "task_id": task.id,
            "user_instruction": user_instruction,
            "direction": direction,
            "constraints": constraints or {},
        },
        status="queued",
        created_at=_now(),
        input_snapshot={
            "series_id": series.id,
            "episode_id": episode.id,
            "episode_number": episode.episode_number,
            "series_state_version": series.state_version,
            "direction": direction,
            "constraints": constraints or {},
        },
    )
    db.add(job)
    db.flush()

    episode.script_status = "draft"
    episode.updated_at = _now()
    db.commit()
    return task.id, job.id


def episode_for_continue(db: Session, episode_id: str) -> SeriesEpisode:
    return get_episode(db, episode_id)
