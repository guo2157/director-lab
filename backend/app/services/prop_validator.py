"""Prop Visual Validator（P1）：分镜关键帧的道具视觉校验。

对应用户规格『Prop Visual Validator（PASS/WARNING/FAIL，失败支持单张重做不整项目重跑）』。

设计约束（与 PROP_BINDING_PLAN / 生命周期规格一致）：
- 校验逻辑纯代码（启发式 + 数据一致性），不进 LLM Prompt，不依赖生图模型自评
- 数据源为 prop_shot_state（每镜道具状态，由 prop_lifecycle.derive_shot_states 推导）
- 语义级检查（道具是否真的出现在画面中、是否拼图）超出本地启发式能力，
  如实记 not_applicable，不伪造通过/失败结论
- 分级：PASS（无问题）/ WARNING（可解释的疑点，不阻塞）/ FAIL（状态矛盾，需处理）
- 失败单张重做：复用既有 POST /api/shots/{shot_id}/regenerate（target=keyframe）
  与资产 regenerate 原地覆盖链路，不整项目重跑

检查项：
1. 技术层：执行器已落库的 Asset.quality_status / quality_issues
   （由 image_quality.check_image_quality 生成时自动检查）
2. 状态一致性层（FAIL 级，纯代码基于 prop_shot_state 与 asset_ref 事实）：
   - 存在状态为 not_introduced/destroyed/consumed，但该镜仍注入了该道具参考图
   - reference_required=True 但该镜参考图缺失（生成时无法锁定外观）
3. 可解释疑点层（WARNING 级）：无关键帧资产 / 质量状态未知 / 可见性未明确
返回结构（按镜头）：
{"shot_id", "verdict": "PASS|WARNING|FAIL", "checks": [...], "redo_hint": {...}|None}
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import (
    Asset,
    AssetRef,
    Prop,
    PropShotState,
    Scene,
    Shot,
)

# 存在状态为「不应出现」的集合（出现即矛盾）
_ABSENT_STATES = {"not_introduced", "destroyed", "consumed"}


@dataclass
class CheckItem:
    name: str
    level: str  # pass / warning / fail / not_applicable
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "level": self.level, "detail": self.detail}


def validate_shot(db: Session, shot_id: str) -> dict:
    """校验单个镜头的关键帧：道具生命周期状态 vs 参考图/质量结果一致性。"""
    shot = db.get(Shot, shot_id)
    if shot is None:
        return {"shot_id": shot_id, "verdict": "FAIL", "checks": [
            CheckItem("shot_exists", "fail", "镜头不存在").to_dict(),
        ], "redo_hint": None}

    checks: list[CheckItem] = []

    # 取证口径（与真实数据一致）：
    # - 关键帧产物经最近已执行任务的 Job.result.assets 定位
    #   （AssetRef 引用在历史清理后未回填，不作为产物存在性依据）
    # - 参考图注入证据以该任务 input_snapshot 实际含 __media__/reference_manifest 为前提；
    #   早期链路生成或重跑队列中的镜头无证据，注入情况记 WARNING（无法核验）而非 FAIL
    import re as _re
    from app.models import Job

    def _latest_job(statuses: tuple[str, ...]) -> Job | None:
        return (
            db.query(Job)
            .filter(
                Job.shot_id == shot.id,
                Job.job_type == "generate_keyframe",
                Job.status.in_(statuses),
            )
            .order_by(Job.created_at.desc())
            .first()
        )

    # 成功任务为产物与注入证据的首选来源；某次重试失败不应遮蔽此前成功产物，
    # 仅当从未成功时才以最近失败任务作为证据（该情况下注入情况无法核验）
    ok_job = _latest_job(("succeeded",))
    latest_job = ok_job or _latest_job(("failed",))
    has_executed = latest_job is not None
    ref_evidence = False  # 取证任务是否留有参考图注入证据
    kf = None
    if ok_job is not None:
        for a in (ok_job.result or {}).get("assets") or []:
            if a.get("asset_type") == "keyframe" and a.get("asset_id"):
                kf = db.get(Asset, a["asset_id"])
                break
    ref_asset_ids: set[str] = {
        r.asset_id
        for r in db.query(AssetRef).filter(AssetRef.shot_id == shot.id).all()
    }
    if latest_job is not None and latest_job.input_snapshot:
        snap = latest_job.input_snapshot
        rm = snap.get("reference_manifest")
        if isinstance(rm, dict):
            entries = rm.get("slots") or []
        elif isinstance(rm, list):
            entries = rm
        else:
            entries = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("asset_id"):
                ref_asset_ids.add(entry["asset_id"])
                ref_evidence = True
        media = snap.get("__media__")
        if isinstance(media, dict):
            for _path in media.values():
                m = _re.search(r"ast_[a-z0-9]+", str(_path))
                if m:
                    ref_asset_ids.add(m.group(0))
                    ref_evidence = True

    if kf is None:
        checks.append(CheckItem("keyframe_exists", "warning", "该镜头暂无关键帧资产（未生成或已清理）"))
    else:
        checks.append(CheckItem("keyframe_exists", "pass", f"关键帧资产 {kf.id}（validity={kf.validity}）"))
        if kf.quality_status == "failed":
            checks.append(CheckItem("image_quality", "fail", f"执行器质量检查不合格: {kf.quality_issues}"))
        elif kf.quality_status == "passed":
            checks.append(CheckItem("image_quality", "pass", "执行器启发式质量检查通过"))
        else:
            checks.append(CheckItem("image_quality", "warning", f"质量状态 {kf.quality_status}: {kf.quality_issues or ''}"))

    # 2. 逐道具状态一致性（纯代码，基于 prop_shot_state 与参考注入事实）
    states = (
        db.query(PropShotState, Prop)
        .join(Prop, PropShotState.prop_id == Prop.id)
        .filter(PropShotState.shot_id == shot.id)
        .all()
    )
    if not states:
        checks.append(CheckItem("prop_states", "not_applicable", "该镜头无道具状态记录（未解析到道具或未推导）"))

    for st, prop in states:
        label = f"prop[{prop.name}]" if prop else f"prop[{st.prop_id}]"
        if st.existence_state in _ABSENT_STATES:
            if st.prop_id in ref_asset_ids:
                checks.append(CheckItem(
                    "state_vs_ref", "fail",
                    f"{label} 状态为 {st.existence_state}（不应出现），但该镜仍注入了其参考图，存在入画风险",
                ))
            else:
                checks.append(CheckItem(
                    "state_vs_ref", "pass",
                    f"{label} 状态为 {st.existence_state}，未注入参考图（负向约束由提示词层负责）",
                ))
            continue
        if st.reference_required:
            if st.prop_id in ref_asset_ids:
                checks.append(CheckItem(
                    "state_vs_ref", "pass",
                    f"{label} visible 且参考图已注入",
                ))
            elif not has_executed or not ref_evidence:
                checks.append(CheckItem(
                    "state_vs_ref", "warning",
                    f"{label} 状态要求出画可锁定，但{'该镜尚无已执行的生成任务（重跑队列中）' if not has_executed else '已执行任务未留有参考图注入证据（早期链路生成）'}，注入情况无法核验，待重跑后复检",
                ))
            else:
                checks.append(CheckItem(
                    "state_vs_ref", "fail",
                    f"{label} 状态要求出画可锁定（visibility={st.visibility}, reference_required）但参考图缺失，生成时无法锁定外观",
                ))
        elif st.visibility == "offscreen":
            checks.append(CheckItem(
                "state_vs_ref", "pass",
                f"{label} 画外（offscreen ≠ absent）：不要求入画，不注入参考图，状态不受影响",
            ))
        else:
            checks.append(CheckItem(
                "state_vs_ref", "warning",
                f"{label} visibility={st.visibility} 未明确要求参考图，请人工确认画面符合剧情",
            ))

    # 3. 语义级检查超出本地启发式能力：如实声明，不伪造结论
    checks.append(CheckItem(
        "semantic_presence", "not_applicable",
        "道具是否真实出现在画面中/是否拼图，超出本地启发式能力，需人工审核或接入视觉模型",
    ))

    levels = {c.level for c in checks}
    if "fail" in levels:
        verdict = "FAIL"
    elif "warning" in levels:
        verdict = "WARNING"
    else:
        verdict = "PASS"

    redo_hint = None
    if verdict == "FAIL" and kf is not None:
        redo_hint = {
            "method": "POST /api/shots/{shot_id}/regenerate",
            "body": {"target": "keyframe"},
            "note": "单张重做走既有 regenerate 链路，不影响其他镜头",
        }

    return {
        "shot_id": shot.id,
        "verdict": verdict,
        "checks": [c.to_dict() for c in checks],
        "redo_hint": redo_hint,
    }


def validate_project_shots(db: Session, project_id: str,
                           episode: int | None = None) -> dict:
    """批量校验：项目（可选按集）全部镜头的道具视觉校验汇总。"""
    q = (
        db.query(Shot.id)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
    )
    if episode is not None:
        q = q.filter(Shot.episode == episode)
    shot_ids = [r[0] for r in q.all()]
    results = [validate_shot(db, sid) for sid in shot_ids]
    summary = {"PASS": 0, "WARNING": 0, "FAIL": 0}
    for r in results:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {
        "project_id": project_id,
        "episode": episode,
        "total": len(results),
        "summary": summary,
        "results": results,
    }
