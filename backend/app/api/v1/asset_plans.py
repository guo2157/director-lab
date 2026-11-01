"""资产定义接口：按解析阶段定稿的定义查询与批量生成。

- GET /api/projects/{id}/asset-plans：查询项目内定义（可按类型/状态过滤），含依赖摘要
- POST /api/projects/{id}/asset-plans/generate：按类型批量触发生成

三个环节的闸口：
- 公共资产（character/scene/prop）：无前置闸口
- 分镜图（keyframe）：须公共资产人工审核通过（D-23），已锁定分镜图跳过
- 分镜视频（clip）：须对应分镜图已人工确认（D-13），且分镜图已生成
"""
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Asset, Character, Job, Project, Prop, Scene, Shot, Workflow
from app.models.asset_plan import AssetPlan, AssetPlanDependency
from app.services import errors

router = APIRouter(prefix="/api/projects", tags=["asset-plans"])

# 定义类型 → 单镜生成 target
SHOT_PLAN_TARGET = {"keyframe": "keyframe", "clip": "clip"}
ENTITY_PLAN_MODEL = {"character": Character, "scene": Scene, "prop": Prop}


# 可注入的参考槽位（ref_prev_frame/first_frame/end_frame 由执行器按镜头实际产物回填）
_REF_SLOTS = ("ref_identity", "ref_costume", "ref_scene", "ref_prop", "ref_image")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_dep_path(db: Session, dep: AssetPlanDependency) -> tuple[str | None, str | None, int | None]:
    """解析依赖槽位对应的实际参考图，返回 (文件路径, 资产ID, 版本)。

    优先级：定义绑定的当前有效资产 → 归属实体的当前资产指针 →
    归属镜头的当前有效资产 → 定义占位路径。
    绑定资产被替换时回退到当前有效资产，
    避免引用过期版本（版本漂移防护）。
    """
    if dep.ref_plan_id:
        ref_plan = db.get(AssetPlan, dep.ref_plan_id)
        if ref_plan is not None:
            if ref_plan.asset_id:
                asset = db.get(Asset, ref_plan.asset_id)
                if asset is not None and asset.file_path:
                    return asset.file_path, asset.id, asset.version
            # 绑定资产缺失或已被替换：按归属类型回退到当前有效资产
            if ref_plan.owner_type in ("character", "scene", "prop") and ref_plan.owner_id:
                model = ENTITY_PLAN_MODEL.get(ref_plan.owner_type)
                if model is not None:
                    entity = db.get(model, ref_plan.owner_id)
                    cur_id = (
                        getattr(entity, "portrait_asset_id", None)
                        or getattr(entity, "baseline_asset_id", None)
                        or getattr(entity, "anchor_asset_id", None)
                    )
                    if cur_id:
                        cur = db.get(Asset, cur_id)
                        if cur is not None and cur.file_path:
                            return cur.file_path, cur.id, cur.version
            if ref_plan.owner_type == "shot" and ref_plan.owner_id:
                cur = (
                    db.query(Asset)
                    .join(Job, Job.id == Asset.source_job_id)
                    .filter(Job.shot_id == ref_plan.owner_id,
                            Asset.asset_type == ref_plan.plan_type,

                            Job.status == "succeeded")
                    .order_by(Asset.version.desc())
                    .first()
                )
                if cur is not None and cur.file_path:
                    return cur.file_path, cur.id, cur.version
            if ref_plan.media_path:
                return ref_plan.media_path, None, None
    return dep.ref_path, None, None


def _inject_dependency_slots(db: Session, plan: AssetPlan, params: dict,
                             allowed_slots: list | None = None) -> dict:
    """按解析阶段定稿的依赖注入参考图槽位，写入 params['__media__']。

    - 槽位由定义绑定决定，不依赖提示词文本的书写顺序，从根本上避免槽位错位
    - allowed_slots 为工作流声明的参考槽位（capabilities.reference_slots）：
      未声明的槽位不注入，避免无效上传与工作流不支持的输入
    - 同一路径只占一个槽位（角色暂无独立服装资产时，ref_costume 与 ref_identity
      指向同一张定妆图，避免被工作流误判为两个不同角色）
    - 绑定快照（槽位/资产ID/版本/路径/状态）随 params 持久化到任务，
      重试复用快照，不重新解析（版本漂移防护）
    - 参考图文件缺失时跳过该槽位并记录 file_missing，不阻断生成
    """
    deps = (
        db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id)
        .all()
    )
    declared = None
    if allowed_slots is not None:
        declared = {
            item.get("param") if isinstance(item, dict) else item
            for item in allowed_slots
        }
    existing = dict(params.get("__media__") or {})
    used_paths = set(existing.values())
    injected: dict[str, str] = {}
    bindings: list[dict] = []
    for dep in deps:
        if dep.slot not in _REF_SLOTS:
            continue
        if declared is not None and dep.slot not in declared:
            continue
        if dep.slot in existing:
            continue
        path, asset_id, version = _resolve_dep_path(db, dep)
        entry = {
            "slot": dep.slot, "ref_key": dep.ref_key,
            "asset_id": asset_id, "version": version, "file_path": path,
        }
        if not path or path in used_paths:
            continue
        if not os.path.exists(path):
            bindings.append({**entry, "status": "file_missing"})
            continue
        injected[dep.slot] = path
        used_paths.add(path)
        bindings.append({**entry, "status": "injected"})
    if injected:
        existing.update(injected)
        params["__media__"] = existing
    if bindings:
        params["__binding_snapshot__"] = bindings
    return injected


def _get_project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise errors.project_not_found()
    return p


def _plan_out(p: AssetPlan, deps: list[AssetPlanDependency] | None = None) -> dict:
    out = {
        "id": p.id,
        "plan_type": p.plan_type,
        "asset_key": p.asset_key,
        "media_path": p.media_path,
        "thumb_path": p.thumb_path,
        "prompt": p.prompt,
        "status": p.status,
        "owner_type": p.owner_type,
        "owner_id": p.owner_id,
        "episode": p.episode,
        "seq": p.seq,
        "asset_id": p.asset_id,
        "version": p.version,
        "confirmed": bool(p.confirmed),
    }
    if deps is not None:
        out["dependencies"] = [
            {"slot": d.slot, "ref_key": d.ref_key, "ref_path": d.ref_path,
             "ref_plan_id": d.ref_plan_id}
            for d in deps
        ]
    return out


@router.get("/{project_id}/asset-plans")
def list_asset_plans(project_id: str, plan_type: str | None = None,
                     status: str | None = None, episode: int | None = None,
                     with_dependencies: bool = False,
                     db: Session = Depends(get_db)) -> dict:
    """查询项目内资产定义，供资产库/分镜图/分镜视频各环节按定义渲染与批量执行。"""
    _get_project(db, project_id)
    q = db.query(AssetPlan).filter(AssetPlan.project_id == project_id)
    if plan_type:
        q = q.filter(AssetPlan.plan_type == plan_type)
    if status:
        q = q.filter(AssetPlan.status == status)
    if episode is not None:
        q = q.filter(AssetPlan.episode == episode)
    plans = q.order_by(AssetPlan.episode, AssetPlan.seq, AssetPlan.id).all()

    deps_map: dict[str, list[AssetPlanDependency]] = {}
    if with_dependencies and plans:
        rows = (
            db.query(AssetPlanDependency)
            .filter(AssetPlanDependency.plan_id.in_([p.id for p in plans]))
            .all()
        )
        for d in rows:
            deps_map.setdefault(d.plan_id, []).append(d)

    counts: dict[str, dict[str, int]] = {}
    for p in plans:
        bucket = counts.setdefault(p.plan_type, {"total": 0, "pending": 0, "generated": 0, "rejected": 0})
        bucket["total"] += 1
        if p.status in bucket:
            bucket[p.status] += 1

    return {
        "items": [_plan_out(p, deps_map.get(p.id)) for p in plans],
        "counts": counts,
        "total": len(plans),
    }


@router.post("/{project_id}/asset-plans/generate")
def generate_from_plans(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """按定义批量触发生成。

    请求体：
    - plan_type：character/scene/prop/keyframe/clip，必填
    - plan_ids：可选，限定具体定义；缺省取该类型全部待生成定义
    - workflow_id：可选，缺省自动匹配
    """
    p = _get_project(db, project_id)
    plan_type = payload.get("plan_type")
    if plan_type not in ("character", "scene", "prop", "keyframe", "clip"):
        raise errors.AppError("invalid_request", "plan_type 仅允许 character/scene/prop/keyframe/clip", 400)

    q = db.query(AssetPlan).filter(
        AssetPlan.project_id == project_id,
        AssetPlan.plan_type == plan_type,
    )
    plan_ids = payload.get("plan_ids")
    if plan_ids:
        q = q.filter(AssetPlan.id.in_(plan_ids))
    else:
        q = q.filter(AssetPlan.status == "pending")
    plans = q.order_by(AssetPlan.episode, AssetPlan.seq).all()
    if not plans:
        raise errors.AppError("no_pending_plans", "没有符合条件且待生成的资产定义", 409)

    workflow_id = payload.get("workflow_id")

    # 分镜图闸口：公共资产须通过人工审核
    if plan_type == "keyframe":
        # 统一资产就绪门禁（D-23 强化）：校验必需资产齐全/有效/文件存在/无在途生成任务/审核通过
        from app.services.assets_gate import assert_assets_gate

        assert_assets_gate(db, project_id)

    from app.api.v1.generate import (  # 复用既有匹配与建任务逻辑，保证行为一致
        _TARGET_WF_TYPE, _check_keyframe_gate, _create_job, _match_workflow, _apply_chain_mode,
        _filter_already_generated,
    )
    # 批量出视频方式（仅 clip）：independent 独立并行 / chained 链式串行（与项目级批量接口一致）
    chain_mode = payload.get("chain_mode") if plan_type == "clip" else None
    if chain_mode is not None and chain_mode not in ("independent", "chained"):
        raise errors.AppError("invalid_request", "chain_mode 仅允许 independent/chained", 400)

    jobs: list[Job] = []
    skipped: list[dict] = []

    if plan_type in SHOT_PLAN_TARGET:
        target = SHOT_PLAN_TARGET[plan_type]
        wf: Workflow = _match_workflow(db, target, workflow_id)
        shot_ids = [x.owner_id for x in plans if x.owner_id]
        if not shot_ids:
            raise errors.AppError("invalid_request", "定义缺少镜头归属，无法生成", 400)
        if target == "clip":
            _check_keyframe_gate(db, shot_ids)
        # 幂等过滤（P0）：同一工作流已生成过视频的镜头跳过，避免重复点击批量出视频时重复渲染；
        # 需要重做时走分镜视频详情页的重新生成，不受此处限制
        _shot_objs = [s for s in (db.get(Shot, sid) for sid in shot_ids) if s is not None]
        _, _already = _filter_already_generated(db, _shot_objs, target, wf.id, wf.version)
        already_done_ids = {d["shot_id"] for d in _already}
        for plan in plans:
            shot = db.get(Shot, plan.owner_id) if plan.owner_id else None
            if shot is None:
                skipped.append({"plan_id": plan.id, "reason": "shot_not_found"})
                continue
            if shot.id in already_done_ids:
                skipped.append({
                    "plan_id": plan.id,
                    "shot_id": shot.id,
                    "reason": "already_generated",
                    "workflow_id": wf.id,
                })
                continue
            # 已锁定分镜图跳过（锁定只作用于图片，不涉及分镜视频）
            if target == "keyframe":
                locked = (
                    db.query(Asset.id)
                    .join(Job, Job.id == Asset.source_job_id)
                    .filter(Job.shot_id == shot.id, Asset.asset_type == "keyframe",
 Asset.locked.is_(True))
                    .first()
                )
                if locked is not None:
                    skipped.append({"plan_id": plan.id, "reason": "asset_locked"})
                    continue
            params: dict = {}
            if plan.prompt:
                params["prompt"] = plan.prompt
            # 参考图按定义绑定注入（与提示词书写顺序无关，不会错槽）；
            # 仅注入当前工作流声明的槽位，绑定快照随任务持久化供追溯与重试复用
            _wf_slots = (wf.capabilities or {}).get("reference_slots") if wf.capabilities else None
            _inject_dependency_slots(db, plan, params, _wf_slots)
            job = _create_job(db, project_id, shot.id, target, wf, params)
            jobs.append(job)
        # 生成方式标记：chained 仅首任务 queued，其余 chained_wait 等待前一任务成功后释放
        if target == "clip" and chain_mode:
            _apply_chain_mode(jobs, chain_mode)
    else:
        target = "keyframe"
        wf = _match_workflow(db, target, workflow_id)
        model = ENTITY_PLAN_MODEL[plan_type]
        owner_key = {"character": "__character_id__", "scene": "__scene_id__", "prop": "__prop_id__"}[plan_type]
        for plan in plans:
            entity = db.get(model, plan.owner_id) if plan.owner_id else None
            if entity is None:
                skipped.append({"plan_id": plan.id, "reason": "owner_not_found"})
                continue
            params = {owner_key: entity.id}
            if plan.prompt:
                params["prompt"] = plan.prompt
            job = _create_job(db, project_id, None, target, wf, params)
            job.params = {**(job.params or {}), owner_key: entity.id}
            jobs.append(job)

    if not jobs:
        db.rollback()
        raise errors.AppError(
            "no_generatable_plans",
            f"没有可生成的定义（跳过 {len(skipped)} 个）",
            409,
        )

    db.commit()
    return {
        "jobs": [{"job_id": j.id, "status": j.status, "job_type": j.job_type} for j in jobs],
        "count": len(jobs),
        "skipped": skipped,
    }


@router.post("/{project_id}/asset-plans/{plan_id}/confirm")
def confirm_plan(project_id: str, plan_id: str, db: Session = Depends(get_db)) -> dict:
    """人工确认某条定义（分镜图确认后放行对应分镜视频生成）。"""
    _get_project(db, project_id)
    plan = db.get(AssetPlan, plan_id)
    if plan is None or plan.project_id != project_id:
        raise errors.AppError("plan_not_found", "资产定义不存在", 404)
    if plan.status != "generated":
        raise errors.AppError("plan_not_generated", "该定义尚未生成产物，无法确认", 409)
    plan.confirmed = True
    # 分镜图确认同步镜头标记，保持既有闸口语义（D-13）
    if plan.plan_type == "keyframe" and plan.owner_id:
        shot = db.get(Shot, plan.owner_id)
        if shot is not None:
            shot.keyframe_confirmed = True
    db.commit()
    return {"plan_id": plan.id, "confirmed": True}


@router.post("/{project_id}/asset-plans/{plan_id}/unconfirm")
def unconfirm_plan(project_id: str, plan_id: str, db: Session = Depends(get_db)) -> dict:
    """取消人工确认。"""
    _get_project(db, project_id)
    plan = db.get(AssetPlan, plan_id)
    if plan is None or plan.project_id != project_id:
        raise errors.AppError("plan_not_found", "资产定义不存在", 404)
    plan.confirmed = False
    if plan.plan_type == "keyframe" and plan.owner_id:
        shot = db.get(Shot, plan.owner_id)
        if shot is not None:
            shot.keyframe_confirmed = False
    db.commit()
    return {"plan_id": plan.id, "confirmed": False}


@router.post("/{project_id}/asset-plans/batch-confirm")
def batch_confirm_plans(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """批量人工确认资产定义（2026-10-28：全流程测试实证逐镜确认效率低，增加批量入口）。

    请求体：{"plan_ids": [id...]}；plan_ids 缺省时确认项目内全部 generated 且未确认的定义。
    跳过非 generated 状态的定义并在响应中列出，不因个别未生成项整体失败。
    """
    _get_project(db, project_id)
    plan_ids = payload.get("plan_ids")
    q = db.query(AssetPlan).filter(AssetPlan.project_id == project_id)
    if plan_ids:
        if not isinstance(plan_ids, list) or not all(isinstance(i, str) for i in plan_ids):
            raise errors.AppError("invalid_request", "plan_ids 必须为字符串数组", 400)
        q = q.filter(AssetPlan.id.in_(plan_ids))
    plans = q.all()
    confirmed: list[str] = []
    skipped: list[dict] = []
    for plan in plans:
        if plan.status != "generated":
            skipped.append({"plan_id": plan.id, "reason": "plan_not_generated", "status": plan.status})
            continue
        if plan.confirmed:
            skipped.append({"plan_id": plan.id, "reason": "already_confirmed"})
            continue
        plan.confirmed = True
        if plan.plan_type == "keyframe" and plan.owner_id:
            shot = db.get(Shot, plan.owner_id)
            if shot is not None:
                shot.keyframe_confirmed = True
        confirmed.append(plan.id)
    db.commit()
    return {"confirmed": confirmed, "confirmed_count": len(confirmed), "skipped": skipped}
