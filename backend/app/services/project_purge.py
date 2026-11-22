"""项目硬删除：彻底清理项目及其全部关联数据与媒体文件。

与 projects.py 中 delete_project（软删除，仅写 deleted_at）不同，本模块执行的是
不可逆的硬删除：按外键依赖自底向上清理所有含 project_id 的表，以及通过
shot_id / job_id / task_id / session_id / plan_id 等间接挂在项目下的表。

设计要点：
- 先冻结检查：存在 queued / running / submission_unknown / chained_wait 任务时默认拒绝（409）；
  调用方显式 force 时先 `cancel_project_active_work` 取消全部在途任务，再执行删除；
- ID 集合全部先收集再删除，避免边删边查导致级联遗漏；
- 删除顺序严格遵循外键依赖（子表在前、父表在后），SQLite 已开启 foreign_keys=ON；
- 每个步骤记录删除行数，返回审计明细供前端展示；
- 媒体文件在数据库事务提交成功后才删除，避免事务回滚后文件已丢失。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from app.models import (
    AgentConfirmation,
    AgentMemory,
    AgentMessage,
    AgentPlanStep,
    AgentSession,
    AgentTask,
    Asset,
    AssetPlan,
    AssetPlanDependency,
    AssetRef,
    AudioReview,
    AudioSegment,
    Character,
    CharacterPropBinding,
    Composition,
    DependencyRef,
    EpisodeContinuity,
    EpisodeParseRecord,
    Export,
    Job,
    JobEvent,
    JobInputRef,
    ParseVersion,
    Project,
    Prop,
    PropEpisodeSnapshot,
    PropLifecycleEvent,
    PropShotState,
    Scene,
    SceneEpisode,
    ScriptEstimate,
    ScriptVersion,
    SearchResult,
    SearchRun,
    SeriesAsset,
    Shot,
    StoryboardRegenConfig,
    Task,
    TaskEvent,
    TaskResult,
    TaskStage,
    VoiceProfile,
)

# 视为「仍在进行」的任务状态：存在任何一个都不允许删除项目
# 单一权威定义在 job_cancel_ops（与批量取消共用），此处仅转发，避免两处口径分叉
from app.services.job_cancel_ops import BLOCKING_JOB_STATUSES  # noqa: E402


def find_blocking_jobs(db: Session, project_id: str) -> int:
    """返回阻碍删除的进行中任务数。"""
    return (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.status.in_(BLOCKING_JOB_STATUSES))
        .count()
    )


# 编排层任务里视为「仍在进行」的状态：不清掉的话 DAG 调度器会继续派发新阶段 → 新 Job
ACTIVE_TASK_STATUSES = ("pending", "queued", "running")
ACTIVE_AGENT_TASK_STATUSES = ("pending", "queued", "running")


def cancel_project_active_work(db: Session, project_id: str, *, reason: str = "project_delete") -> dict:
    """取消项目下所有在途工作，供 force 删除使用（删除前必须调用）。

    Job 层的取消动作统一走 ``job_cancel_ops.cancel_jobs``（唯一权威实现：
    /interrupt + 清 prompt_id + 事件 + 编排层桥接 + 跨进程信号）。
    这里额外负责「编排层不能继续派发」这一层：

    为什么还要单独取消 Task / AgentTask
    ---------------------------------
    只改 Job 状态不够：Job 由编排层 Task / AgentTask 派发，任务没停，
    DAG 调度器会在下一个 tick 继续启动 pending 阶段、再派发新 Job，
    于是「刚取消完又有新任务排队」，删除永远追不上。
    所以三层一起置终态：Job（执行单元）→ Task（统一编排）→ AgentTask（意图任务）。

    返回值供前端展示「本次取消了几个任务」，不影响删除流程。
    """
    from app.services.job_cancel_ops import cancel_jobs

    jobs = (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.status.in_(BLOCKING_JOB_STATUSES))
        .all()
    )
    stats = cancel_jobs(db, jobs, reason=reason)

    cancelled_tasks = 0
    try:
        from app.services import task_orchestrator

        tasks = (
            db.query(Task)
            .filter(Task.project_id == project_id, Task.status.in_(ACTIVE_TASK_STATUSES))
            .all()
        )
        for t in tasks:
            if task_orchestrator.cancel_task(db, t, reason=reason):
                cancelled_tasks += 1
    except Exception:  # noqa: BLE001
        pass

    cancelled_agent_tasks = 0
    session_ids = [
        r[0]
        for r in db.execute(select(AgentSession.id).where(AgentSession.project_id == project_id))
    ]
    if session_ids:
        try:
            from app.services import parent_agent

            agent_tasks = (
                db.query(AgentTask)
                .filter(
                    AgentTask.session_id.in_(session_ids),
                    AgentTask.status.in_(ACTIVE_AGENT_TASK_STATUSES),
                )
                .all()
            )
            for at in agent_tasks:
                if parent_agent.cancel_task(db, at, reason=reason):
                    cancelled_agent_tasks += 1
        except Exception:  # noqa: BLE001
            pass

    db.commit()

    return {
        "jobs": stats["jobs"],
        "running": stats["running"],
        "interrupted": stats["interrupted"],
        "tasks": cancelled_tasks,
        "agent_tasks": cancelled_agent_tasks,
    }


def count_project_data(db: Session, project_id: str) -> dict:
    """统计项目关联数据量，用于删除前的影响面预览。"""
    scene_ids = [r[0] for r in db.execute(select(Scene.id).where(Scene.project_id == project_id))]
    shot_ids = (
        [r[0] for r in db.execute(select(Shot.id).where(Shot.scene_id.in_(scene_ids)))]
        if scene_ids
        else []
    )
    return {
        "scenes": len(scene_ids),
        "shots": len(shot_ids),
        "assets": db.query(Asset).filter(Asset.project_id == project_id).count(),
        "jobs": db.query(Job).filter(Job.project_id == project_id).count(),
        "characters": db.query(Character).filter(Character.project_id == project_id).count(),
        "props": db.query(Prop).filter(Prop.project_id == project_id).count(),
        "script_versions": db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .count(),
    }


def purge_project(db: Session, project_id: str) -> dict:
    """硬删除项目及其全部关联数据。调用方负责异常处理与事务提交。

    返回 {'project_id':..., 'project_name':..., 'deleted': {表名: 行数}, 'media_paths': [...]}
    """
    project = db.get(Project, project_id)
    if project is None:
        raise LookupError(project_id)

    project_name = project.name
    deleted: dict[str, int] = {}

    def _delete(stmt, label: str) -> int:
        result = db.execute(stmt)
        rows = result.rowcount or 0
        if rows:
            deleted[label] = deleted.get(label, 0) + rows
        return rows

    # ── 1. 收集 ID 集合（先收集后删除，避免级联遗漏） ──────────────
    scene_ids = [r[0] for r in db.execute(select(Scene.id).where(Scene.project_id == project_id))]
    shot_ids = (
        [r[0] for r in db.execute(select(Shot.id).where(Shot.scene_id.in_(scene_ids)))]
        if scene_ids
        else []
    )
    asset_ids = [r[0] for r in db.execute(select(Asset.id).where(Asset.project_id == project_id))]
    job_ids = [r[0] for r in db.execute(select(Job.id).where(Job.project_id == project_id))]
    plan_ids = [
        r[0] for r in db.execute(select(AssetPlan.id).where(AssetPlan.project_id == project_id))
    ]
    task_ids = [r[0] for r in db.execute(select(Task.id).where(Task.project_id == project_id))]
    session_ids = [
        r[0]
        for r in db.execute(select(AgentSession.id).where(AgentSession.project_id == project_id))
    ]
    agent_task_ids = (
        [r[0] for r in db.execute(select(AgentTask.id).where(AgentTask.session_id.in_(session_ids)))]
        if session_ids
        else []
    )
    script_ids = [
        r[0]
        for r in db.execute(select(ScriptVersion.id).where(ScriptVersion.project_id == project_id))
    ]
    search_ids = [
        r[0] for r in db.execute(select(SearchRun.id).where(SearchRun.project_id == project_id))
    ]

    # 媒体文件路径先收集，待事务提交后再删文件
    media_paths: list[str] = []
    for file_path, thumb_path in db.execute(
        select(Asset.file_path, Asset.thumb_path).where(Asset.project_id == project_id)
    ):
        if file_path:
            media_paths.append(file_path)
        if thumb_path:
            media_paths.append(thumb_path)

    # ══ 2. 叶子表：完全依赖 job / task / agent_task 等 ══════════
    if job_ids:
        _delete(delete(JobEvent).where(JobEvent.job_id.in_(job_ids)), "job_event")
        _delete(delete(JobInputRef).where(JobInputRef.job_id.in_(job_ids)), "job_input_ref")
    if task_ids:
        _delete(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)), "task_event")
        _delete(delete(TaskResult).where(TaskResult.task_id.in_(task_ids)), "task_result")
        _delete(delete(TaskStage).where(TaskStage.task_id.in_(task_ids)), "task_stage")
    # AgentPlanStep 的外键是 task_id（指向 agent_task.id），不是 session_id
    if agent_task_ids:
        _delete(
            delete(AgentPlanStep).where(AgentPlanStep.task_id.in_(agent_task_ids)),
            "agent_plan_step",
        )

    # ══ 3. 引用关系表：跨 shot / asset ═══════════════════════════
    if shot_ids or asset_ids:
        ref_cond = []
        if shot_ids:
            ref_cond.append(AssetRef.shot_id.in_(shot_ids))
        if asset_ids:
            ref_cond.append(AssetRef.asset_id.in_(asset_ids))
        if ref_cond:
            _delete(delete(AssetRef).where(or_(*ref_cond)), "asset_ref")

        dep_cond = []
        if asset_ids:
            dep_cond.append(DependencyRef.source_id.in_(asset_ids))
            dep_cond.append(DependencyRef.target_id.in_(asset_ids))
        if shot_ids:
            dep_cond.append(DependencyRef.source_id.in_(shot_ids))
            dep_cond.append(DependencyRef.target_id.in_(shot_ids))
        if dep_cond:
            _delete(delete(DependencyRef).where(or_(*dep_cond)), "dependency_ref")

    # ══ 4. 音频：AudioReview → AudioSegment（顺序不可颠倒） ═══════
    _delete(delete(AudioReview).where(AudioReview.project_id == project_id), "audio_review")
    if shot_ids:
        _delete(delete(AudioSegment).where(AudioSegment.shot_id.in_(shot_ids)), "audio_segment")
    _delete(delete(AudioSegment).where(AudioSegment.project_id == project_id), "audio_segment")

    # ══ 5. 道具派生状态（依赖 shot / prop） ══════════════════════
    if shot_ids:
        _delete(delete(PropShotState).where(PropShotState.shot_id.in_(shot_ids)), "prop_shot_state")
    _delete(
        delete(PropShotState).where(PropShotState.project_id == project_id), "prop_shot_state"
    )
    _delete(
        delete(PropEpisodeSnapshot).where(PropEpisodeSnapshot.project_id == project_id),
        "prop_episode_snapshot",
    )
    _delete(
        delete(PropLifecycleEvent).where(PropLifecycleEvent.project_id == project_id),
        "prop_lifecycle_event",
    )
    _delete(
        delete(CharacterPropBinding).where(CharacterPropBinding.project_id == project_id),
        "character_prop_binding",
    )

    # ══ 6. 计划与估算（依赖 asset_plan / script_version） ════════
    if plan_ids:
        _delete(
            delete(AssetPlanDependency).where(AssetPlanDependency.plan_id.in_(plan_ids)),
            "asset_plan_dependency",
        )
    if script_ids:
        _delete(
            delete(ScriptEstimate).where(ScriptEstimate.script_version_id.in_(script_ids)),
            "script_estimate",
        )

    # ══ 7. 检索结果 ═════════════════════════════════════════════
    if search_ids:
        _delete(delete(SearchResult).where(SearchResult.search_run_id.in_(search_ids)), "search_result")

    # ══ 8. 成片与导出：Export → Composition ═════════════════════
    _delete(delete(Export).where(Export.project_id == project_id), "export")
    _delete(delete(Composition).where(Composition.project_id == project_id), "composition")

    # ══ 8.5 分镜重新生成工作台配置：shot_id → shot.id 外键，必须先于镜头删除。
    # 2026-11-21 真实事故：该表是后加的，purge 最初没覆盖它，删除镜头时报
    # FOREIGN KEY constraint failed（狼来了无法删除，500）。project_id 直挂，按项目清即可。
    _delete(
        delete(StoryboardRegenConfig).where(StoryboardRegenConfig.project_id == project_id),
        "storyboard_regen_config",
    )

    # ══ 9. 任务必须早于镜头删除（job.shot_id → shot.id） ═════════
    _delete(delete(Job).where(Job.project_id == project_id), "job")
    _delete(delete(Task).where(Task.project_id == project_id), "task")

    # ══ 9.5 系列共享资产关联：asset_id → asset.id 外键，必须先于资产删除。
    # 系列资产引用属于系列侧、但被删项目的资产一旦被纳入系列，purge 资产时会 FK 失败。
    if asset_ids:
        _delete(delete(SeriesAsset).where(SeriesAsset.asset_id.in_(asset_ids)), "series_asset")

    # ══ 10. 资产计划与资产 ══════════════════════════════════════
    _delete(delete(AssetPlan).where(AssetPlan.project_id == project_id), "asset_plan")
    _delete(delete(Asset).where(Asset.project_id == project_id), "asset")

    # ══ 11. 镜头 → 场景集关联 → 场景 ════════════════════════════
    if scene_ids:
        _delete(delete(Shot).where(Shot.scene_id.in_(scene_ids)), "shot")
        _delete(delete(SceneEpisode).where(SceneEpisode.scene_id.in_(scene_ids)), "scene_episode")
    _delete(delete(Scene).where(Scene.project_id == project_id), "scene")

    # ══ 12. 解析版本（依赖 script_version）→ 剧本 → 解析记录 ═════
    _delete(delete(ParseVersion).where(ParseVersion.project_id == project_id), "parse_version")
    _delete(delete(ScriptVersion).where(ScriptVersion.project_id == project_id), "script_version")
    _delete(
        delete(EpisodeParseRecord).where(EpisodeParseRecord.project_id == project_id),
        "episode_parse_record",
    )
    _delete(
        delete(EpisodeContinuity).where(EpisodeContinuity.project_id == project_id),
        "episode_continuity",
    )
    # series_episodes 为 project_id 直挂表，当前多为 0 行仍需显式清理
    tables = Project.__table__.metadata.tables
    series_episodes = tables.get("series_episodes")
    if series_episodes is not None and "project_id" in series_episodes.columns:
        episode_ids = [
            r[0]
            for r in db.execute(
                select(series_episodes.c.id).where(series_episodes.c.project_id == project_id)
            )
        ]
        # 集级子表以 episode_id 外键挂在 series_episodes 上，必须先删子表再删集
        for name in (
            "series_script_versions",
            "episode_asset_refs",
            "story_events",
            "continuity_change_proposals",
            "episode_context_manifests",
        ):
            tbl = tables.get(name)
            if episode_ids and tbl is not None and "episode_id" in tbl.columns:
                _delete(delete(tbl).where(tbl.c.episode_id.in_(episode_ids)), name)
        _delete(
            delete(series_episodes).where(series_episodes.c.project_id == project_id),
            "series_episode",
        )
        # 系列上的 latest_* 指针会指向被删掉的剧集，必须一并清空，
        # 否则 /series/:id 与 /episodes/:id/review 会拿到悬空 episode_id（报「剧集不存在」）。
        if episode_ids:
            series_tbl = tables.get("series")
            if series_tbl is not None:
                for col in ("latest_confirmed_episode_id", "latest_completed_episode_id"):
                    if col in series_tbl.columns:
                        db.execute(
                            series_tbl.update()
                            .where(series_tbl.c[col].in_(episode_ids))
                            .values({col: None})
                        )

    # ══ 13. Agent 链路：Message / Task → Session ════════════════
    _delete(
        delete(AgentConfirmation).where(AgentConfirmation.project_id == project_id),
        "agent_confirmation",
    )
    _delete(delete(AgentMemory).where(AgentMemory.project_id == project_id), "agent_memory")
    if session_ids:
        _delete(delete(AgentMessage).where(AgentMessage.session_id.in_(session_ids)), "agent_message")
        _delete(delete(AgentTask).where(AgentTask.session_id.in_(session_ids)), "agent_task")
    _delete(delete(AgentSession).where(AgentSession.project_id == project_id), "agent_session")
    _delete(delete(SearchRun).where(SearchRun.project_id == project_id), "search_run")

    # ══ 14. 角色与道具（需在 shot / voice_profile 之后） ════════
    _delete(delete(VoiceProfile).where(VoiceProfile.project_id == project_id), "voice_profile")
    _delete(delete(Character).where(Character.project_id == project_id), "character")
    _delete(delete(Prop).where(Prop.project_id == project_id), "prop")

    # ══ 15. 项目本体 ════════════════════════════════════════════
    _delete(delete(Project).where(Project.id == project_id), "project")

    return {
        "project_id": project_id,
        "project_name": project_name,
        "deleted": deleted,
        "media_paths": media_paths,
    }


def remove_media_files(paths: list[str], media_root: Path) -> dict:
    """删除媒体文件，仅允许 media_root 之下的路径，防止越权删除。

    返回 {'removed': n, 'missing': n, 'skipped': n}
    """
    removed = missing = skipped = 0
    root = media_root.resolve()
    for raw in paths:
        if not raw:
            continue
        try:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = media_root / candidate
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                skipped += 1
                continue
            if resolved.is_file():
                resolved.unlink()
                removed += 1
            else:
                missing += 1
        except OSError:
            skipped += 1
    return {"removed": removed, "missing": missing, "skipped": skipped}
