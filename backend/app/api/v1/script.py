"""剧本与解析接口：API.md §5 契约。
- script-versions：保存剧本文本并创建不可变版本
- estimate：评估可制作集数（同步，不调语言模型，结果持久化）
- estimates/{id}/confirm：确认评估与制作范围
- parse：解析剧本（202 长任务，入队后由 worker 调语言模型）
- parse-versions/{id}/confirm：确认候选解析版本（乐观锁原子切换）
纪律：draft 项目首次调用 parse 自动激活（ARCHITECTURE.md §2）；
评估/解析均校验 text 哈希与 script_version_id 一致，不符 409 script_version_conflict。
"""
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Job, ParseVersion, Project, ScriptEstimate, ScriptVersion
from app.models import ids
from app.schemas.script import (
    EstimateConfirm, EstimateConfirmOut, EstimateCreate, EstimateOut,
    ParseCreate, ParseJobOut, ParseVersionConfirm, ParseVersionOut,
    ScriptVersionCreate, ScriptVersionOut,
)
from app.services import errors
from app.services.script_parse import estimate_script

router = APIRouter(prefix="/api/projects", tags=["script"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get_project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise errors.project_not_found()
    return p


def _get_script_version(db: Session, sv_id: str) -> ScriptVersion:
    sv = db.get(ScriptVersion, sv_id)
    if sv is None:
        raise errors.script_version_not_found()
    return sv


def _check_text_hash(sv: ScriptVersion, text: str) -> None:
    if _hash(text) != sv.content_hash:
        raise errors.script_version_conflict("text 内容哈希与剧本版本不一致")


def _activate_if_draft(db: Session, p: Project) -> None:
    if p.status == "draft":
        p.status = "active"


@router.post("/{project_id}/script-versions", status_code=201)
def create_script_version(project_id: str, payload: ScriptVersionCreate,
                          db: Session = Depends(get_db)) -> ScriptVersionOut:
    p = _get_project(db, project_id)
    max_ver = (
        db.query(ScriptVersion.version)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )
    next_ver = (max_ver[0] if max_ver else 0) + 1
    sv = ScriptVersion(
        id=ids.script_version_id(), project_id=project_id, version=next_ver,
        content_snapshot=payload.text, content_hash=_hash(payload.text), created_at=_now(),
    )
    db.add(sv)
    p.current_script_version_id = sv.id
    db.commit()
    return ScriptVersionOut(script_version_id=sv.id, version=next_ver, content_hash=sv.content_hash)


@router.post("/{project_id}/estimate", status_code=201)
def create_estimate(project_id: str, payload: EstimateCreate,
                    db: Session = Depends(get_db)) -> EstimateOut:
    p = _get_project(db, project_id)
    sv = _get_script_version(db, payload.script_version_id)
    if sv.project_id != project_id:
        raise errors.script_version_conflict("剧本版本不属于本项目")
    _check_text_hash(sv, payload.text)
    settings_map = p.settings or {}
    result = estimate_script(
        payload.text,
        episode_duration_target=int(settings_map.get("episode_duration_target", 90)),
        max_episodes_per_batch=int(settings_map.get("max_episodes_per_batch", 10)),
    )
    est = ScriptEstimate(
        id=ids.script_estimate_id(), script_version_id=sv.id,
        algorithm_version=result["algorithm_version"], result=result,
        user_confirmed=False, created_at=_now(),
    )
    db.add(est)
    p.estimated_at = _now()
    p.estimated_text_hash = sv.content_hash
    db.commit()
    return EstimateOut(
        estimate_id=est.id, script_version_id=sv.id,
        algorithm_version=result["algorithm_version"],
        char_count=result["char_count"], dialogue_char_estimate=result["dialogue_char_estimate"],
        estimated_total_duration=result["estimated_total_duration"],
        estimated_episodes=result["estimated_episodes"],
        episode_breakdown=result["episode_breakdown"],
        exceeds_batch_limit=result["exceeds_batch_limit"],
        suggestion=result["suggestion"], user_confirmed=False,
    )


@router.post("/{project_id}/estimates/{estimate_id}/confirm")
def confirm_estimate(project_id: str, estimate_id: str, payload: EstimateConfirm,
                     db: Session = Depends(get_db)) -> EstimateConfirmOut:
    _get_project(db, project_id)
    est = db.get(ScriptEstimate, estimate_id)
    if est is None:
        raise errors.estimate_not_found()
    if est.script_version_id != payload.script_version_id:
        raise errors.script_version_conflict("评估版本与确认的剧本版本不一致")
    est.user_confirmed = True
    db.commit()
    return EstimateConfirmOut(
        estimate_id=est.id, user_confirmed=True,
        episode_start=payload.episode_start, episode_count=payload.episode_count,
    )


@router.post("/{project_id}/parse", status_code=202)
def create_parse(project_id: str, payload: ParseCreate,
                 db: Session = Depends(get_db)) -> ParseJobOut:
    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可解析")
    sv = _get_script_version(db, payload.script_version_id)
    if sv.project_id != project_id:
        raise errors.script_version_conflict("剧本版本不属于本项目")
    _check_text_hash(sv, payload.text)
    # 前置：必须已确认与 script_version_id 匹配的评估
    confirmed = (
        db.query(ScriptEstimate)
        .filter(ScriptEstimate.script_version_id == sv.id, ScriptEstimate.user_confirmed.is_(True))
        .first()
    )
    if confirmed is None:
        raise errors.invalid_request("解析前须先评估并确认与剧本版本匹配的制作范围")
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="parse",
        params={
            "script_version_id": sv.id,
            "episode_start": payload.episode_start,
            "episode_count": payload.episode_count,
        },
        status="queued", created_at=_now(),
    )
    db.add(job)
    _activate_if_draft(db, p)
    db.commit()
    return ParseJobOut(job_id=job.id, status=job.status)


@router.post("/{project_id}/parse-versions/{parse_version_id}/confirm")
def confirm_parse_version(project_id: str, parse_version_id: str, payload: ParseVersionConfirm,
                          db: Session = Depends(get_db)) -> ParseVersionOut:
    p = _get_project(db, project_id)
    pv = db.get(ParseVersion, parse_version_id)
    if pv is None or pv.project_id != project_id:
        raise errors.parse_version_not_found()
    if pv.status != "candidate":
        raise errors.parse_version_conflict("仅候选状态的解析版本可确认")
    # 乐观锁：请求携带的当前版本须与实际一致
    if (payload.current_parse_version_id or None) != (p.current_parse_version_id or None):
        raise errors.parse_version_conflict()
    # 找到产生该候选版本的解析任务，取其 result 做级联落库
    parse_job = (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.job_type == "parse", Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .all()
    )
    target_result = None
    for j in parse_job:
        if (j.result or {}).get("parse_version_id") == pv.id:
            target_result = j.result
            break
    if target_result is not None:
        from app.models import Scene as SceneModel
        from app.services.script_parse import apply_parse_result
        current_scene_ids = [
            row[0] for row in db.query(SceneModel.id).filter(SceneModel.project_id == project_id).all()
        ]
        apply_parse_result(
            db, p, target_result,
            episode_start=int(target_result.get("episode_start") or 1),
            episode_count=int(target_result.get("episode_count") or 1),
            current_scene_ids=current_scene_ids,
        )
    # 旧版本标记 superseded
    old_id = p.current_parse_version_id
    if old_id:
        old = db.get(ParseVersion, old_id)
        if old is not None and old.status == "current":
            old.status = "superseded"
    pv.status = "current"
    pv.confirmed_at = _now()
    p.current_parse_version_id = pv.id
    db.commit()
    return ParseVersionOut(
        parse_version_id=pv.id, version=pv.version, status=pv.status, diff_summary=pv.diff_summary,
    )
