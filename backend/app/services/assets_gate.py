"""统一资产就绪门禁（D-23 强化）：所有分镜图生成入口共用的后端闸口。

核心规则：所有必需角色、场景、道具均已生成完成，并且人工审核通过后，
才允许开始分镜图生成。

与 assets_review.py 的分工：
- assets_review 负责『审核状态回退』（资产变更后把项目审核状态打回 pending）；
- assets_gate 负责『就绪判定』（每次创建/执行分镜图生成任务前，基于真实
  资产数据重新计算是否放行，并输出结构化阻断明细）。

设计约束：
- 就绪状态由后端根据实际资产与审核数据计算，前端传入的任何标志不可绕过；
- 必需性规则沿用现有模型：角色定妆图、场景基准图、anchor_required=True 的
  道具锚定图为必需资产，可选道具不作为阻断条件；
- 检查的是资产真实状态（指针、validity、文件存在、在途任务），不是记录存在性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Asset, Character, Job, Project, Prop, Scene

# 就绪状态（由后端计算，禁止前端直传）
GATE_READY = "ASSETS_READY"
GATE_INCOMPLETE = "ASSETS_INCOMPLETE"
GATE_GENERATING = "ASSETS_GENERATING"
GATE_PENDING_REVIEW = "ASSETS_PENDING_REVIEW"
GATE_REJECTED = "ASSETS_REJECTED"

# 阻断原因 → 建议操作
_SUGGESTIONS = {
    "asset_missing": "请在资产库生成该资产后再提交审核",
    "asset_generating": "请等待该资产生成任务完成后再提交审核",
    "asset_invalid": "该资产版本已失效，请重新生成或回滚到有效版本",
    "asset_file_missing": "资产文件缺失，请重新生成该资产",
    "review_pending": "请在资产库完成人工审核（审核通过）后再生成分镜图",
    "review_rejected": "请在资产库修改或替换被驳回的资产后重新提交审核",
}


@dataclass
class GateItem:
    """单个不合格资产的阻断明细。"""

    asset_id: str | None
    name: str
    asset_type: str  # character / scene / prop
    reason: str  # asset_missing / asset_generating / asset_invalid / asset_file_missing
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "asset_type": self.asset_type,
            "reason": self.reason,
            "detail": self.detail,
            "suggestion": _SUGGESTIONS.get(self.reason, ""),
        }


@dataclass
class GateResult:
    """门禁判定结果：ready + 状态 + 阻断明细。"""

    ready: bool
    status: str
    reason_code: str  # assets_incomplete / assets_generating / assets_review_pending / assets_review_rejected
    message: str
    blockers: list[GateItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "blockers": [b.to_dict() for b in self.blockers],
        }


def _media_file_exists(file_path: str | None) -> bool:
    """校验资产文件真实存在。

    兼容两种存储口径：file_path 已含 media_dir 前缀（如 data/media/xxx.png，
    相对后端工作目录，executor 落库口径）时按原样解析；为裸文件名时再按
    media_dir 拼接。避免 media_dir / file_path 产生双重前缀导致校验误报。
    """
    if not file_path:
        return False
    p = Path(file_path)
    if not p.is_absolute():
        if p.exists():
            return True
        p = get_settings().media_dir / p
    return p.exists()


def _check_entity_assets(
    db: Session,
    project_id: str,
) -> list[GateItem]:
    """逐个校验必需资产的真实状态：指针 → validity → 文件存在。"""
    blockers: list[GateItem] = []

    characters = db.query(Character).filter(Character.project_id == project_id).all()
    for c in characters:
        if not c.portrait_asset_id:
            blockers.append(
                GateItem(None, c.name, "character", "asset_missing", "角色缺少定妆图")
            )
            continue
        a = db.get(Asset, c.portrait_asset_id)
        if a is None or a.validity != "current":
            blockers.append(
                GateItem(c.portrait_asset_id, c.name, "character", "asset_invalid", "定妆图版本已失效")
            )
            continue
        if not _media_file_exists(a.file_path):
            blockers.append(
                GateItem(a.id, c.name, "character", "asset_file_missing", f"定妆图文件缺失：{a.file_path}")
            )

    scenes = db.query(Scene).filter(Scene.project_id == project_id).all()
    for sc in scenes:
        if not sc.baseline_asset_id:
            blockers.append(GateItem(None, sc.title or sc.id, "scene", "asset_missing", "场景缺少基准图"))
            continue
        a = db.get(Asset, sc.baseline_asset_id)
        if a is None or a.validity != "current":
            blockers.append(
                GateItem(sc.baseline_asset_id, sc.title or sc.id, "scene", "asset_invalid", "基准图版本已失效")
            )
            continue
        if not _media_file_exists(a.file_path):
            blockers.append(
                GateItem(a.id, sc.title or sc.id, "scene", "asset_file_missing", f"基准图文件缺失：{a.file_path}")
            )

    props = db.query(Prop).filter(Prop.project_id == project_id, Prop.anchor_required.is_(True)).all()
    for p in props:
        if not p.anchor_asset_id:
            blockers.append(GateItem(None, p.name, "prop", "asset_missing", "必需道具缺少锚定图"))
            continue
        a = db.get(Asset, p.anchor_asset_id)
        if a is None or a.validity != "current":
            blockers.append(GateItem(p.anchor_asset_id, p.name, "prop", "asset_invalid", "锚定图版本已失效"))
            continue
        if not _media_file_exists(a.file_path):
            blockers.append(
                GateItem(a.id, p.name, "prop", "asset_file_missing", f"锚定图文件缺失：{a.file_path}")
            )

    return blockers


def _has_inflight_asset_jobs(db: Session, project_id: str) -> bool:
    """项目内是否仍有公共资产生成任务在途（queued/running/submission_unknown）。

    资产生成任务的特征：job_type=generate_keyframe 且 params 携带
    __character_id__ / __scene_id__ / __prop_id__ 之一（与 generate.py 落库口径一致）。
    """
    rows = (
        db.query(Job.params)
        .filter(
            Job.project_id == project_id,
            Job.job_type == "generate_keyframe",
            Job.status.in_(["queued", "running", "submission_unknown"]),
        )
        .all()
    )
    for (params,) in rows:
        if not isinstance(params, dict):
            continue
        if any(k in params for k in ("__character_id__", "__scene_id__", "__prop_id__")):
            return True
    return False


def evaluate_assets_gate(db: Session, project_id: str) -> GateResult:
    """计算项目资产就绪状态（每次调用基于实时数据，不缓存结论）。

    判定顺序（先资产后审核，保证错误信息指向最前置的阻断原因）：
    1. 必需资产存在且有效（缺失/失效/文件缺失 → ASSETS_INCOMPLETE）
    2. 无在途资产生成任务（有 → ASSETS_GENERATING）
    3. 项目审核状态 approved（pending/rejected → 对应状态）
    """
    project = db.get(Project, project_id)
    if project is None:
        return GateResult(
            False,
            GATE_INCOMPLETE,
            "project_not_found",
            "项目不存在",
        )

    blockers = _check_entity_assets(db, project_id)
    if blockers:
        names = "、".join(f"{b.name}({b.asset_type})" for b in blockers[:5])
        more = f" 等 {len(blockers)} 项" if len(blockers) > 5 else ""
        return GateResult(
            False,
            GATE_INCOMPLETE,
            "assets_incomplete",
            f"必需资产不完整或无效（{names}{more}），请先在资产库补齐并重新提交审核",
            blockers,
        )

    if _has_inflight_asset_jobs(db, project_id):
        return GateResult(
            False,
            GATE_GENERATING,
            "assets_generating",
            "必需资产仍在生成中，请等待生成任务完成后再生成分镜图",
        )

    status = project.assets_review_status or "pending"
    if status == "rejected":
        return GateResult(
            False,
            GATE_REJECTED,
            "assets_review_rejected",
            "公共资产审核未通过，请在资产库修改或替换不满意的资产后重新提交人工审核",
        )
    if status != "approved":
        return GateResult(
            False,
            GATE_PENDING_REVIEW,
            "assets_review_pending",
            "公共资产尚未完成人工审核，请在资产库确认资产无误并通过审核后再生成分镜图",
        )

    return GateResult(True, GATE_READY, "", "")


def assert_assets_gate(db: Session, project_id: str) -> None:
    """门禁断言：未就绪时抛出结构化 AppError（含阻断明细）。

    所有创建分镜图生成任务的入口（API / 批量 / Agent）统一调用本函数，
    错误码沿用 D-23 既有约定，payload 携带 blockers 明细。
    """
    from app.services import errors

    result = evaluate_assets_gate(db, project_id)
    if result.ready:
        return
    raise errors.AppError(
        result.reason_code,
        result.message,
        409,
        blockers=[b.to_dict() for b in result.blockers],
        gate_status=result.status,
    )
