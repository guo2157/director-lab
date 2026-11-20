"""统一资产就绪门禁：所有分镜图生成入口共用的后端闸口。

核心规则（2026-11 简化）：所有必需角色、场景、道具的图片均已**真实生成完成**
且文件有效，并且没有正在生成的必需资产，才允许开始分镜图生成。

人工审核闸口已取消：不再读 assets_review_status，不再有
ASSETS_PENDING_REVIEW / ASSETS_REJECTED 这类阻断原因。阶段推进的把关动作
改为前端点击「去分镜图」时的一次确认弹窗（用户自查承诺），后端只负责
权威数据校验——图片是否真的生成、文件是否真的存在、版本是否仍然有效。

设计约束：
- 就绪状态由后端根据实际资产数据计算，前端传入的任何标志不可绕过；
- 必需性规则沿用现有模型：角色定妆图、场景基准图、anchor_required=True 的
  道具锚定图为必需资产，可选道具不作为阻断条件；
- 检查的是资产真实状态（指针、版本有效性、文件存在、在途任务），
  不是记录存在性；
- 返回值携带分项统计（角色/场景/道具各自的 total/done），供前端展示
  「角色 5/5 场景 3/4 道具 2/3」这类进度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings, stored_path
from app.models import Asset, Character, Job, Project, Prop, Scene

# 就绪状态（由后端计算，禁止前端直传）
GATE_READY = "ASSETS_READY"
GATE_INCOMPLETE = "ASSETS_INCOMPLETE"
GATE_GENERATING = "ASSETS_GENERATING"

# 阻断原因 → 建议操作（不含任何审核相关表述：审核已取消）
_SUGGESTIONS = {
    "asset_missing": "请在资产库生成该资产图片",
    "asset_generating": "请等待该资产生成任务完成",
    "asset_invalid": "该资产版本已失效，请重新生成",
    "asset_file_missing": "资产文件缺失，请重新生成该资产",
}

# 资产类型 → 中文名（前端文案与后端统计共用同一口径）
ASSET_TYPE_LABELS = {"character": "角色", "scene": "场景", "prop": "道具"}


@dataclass
class GateItem:
    """单个不合格资产的阻断明细。"""

    asset_id: str | None
    name: str
    asset_type: str  # character / scene / prop
    reason: str  # asset_missing / asset_generating / asset_invalid / asset_file_missing
    detail: str = ""
    entity_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "entity_id": self.entity_id,
            "name": self.name,
            "asset_type": self.asset_type,
            "asset_type_label": ASSET_TYPE_LABELS.get(self.asset_type, self.asset_type),
            "reason": self.reason,
            "detail": self.detail,
            "suggestion": _SUGGESTIONS.get(self.reason, ""),
        }


@dataclass
class GateResult:
    """门禁判定结果：ready + 状态 + 分项统计 + 阻断明细。"""

    ready: bool
    status: str
    reason_code: str  # assets_incomplete / assets_generating / project_not_found
    message: str
    blockers: list[GateItem] = field(default_factory=list)
    summary: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(g.get("total", 0) for g in self.summary.values())

    @property
    def done(self) -> int:
        return sum(g.get("done", 0) for g in self.summary.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "total": self.total,
            "done": self.done,
            "summary": self.summary,
            "blockers": [b.to_dict() for b in self.blockers],
        }


def media_file_exists(file_path: str | None) -> bool:
    """校验资产文件真实存在。

    兼容两种存储口径：file_path 已含 media_dir 前缀（如 data/media/xxx.png，
    相对后端工作目录，executor 落库口径）时按原样解析；为裸文件名时再按
    media_dir 拼接。避免 media_dir / file_path 产生双重前缀导致校验误报。
    """
    if not file_path:
        return False
    p = stored_path(file_path)
    if not p.is_absolute():
        if p.exists():
            return True
        p = get_settings().media_dir / p
    return p.exists()


# 兼容旧调用名（其他模块历史上引用的是私有名）
_media_file_exists = media_file_exists


def _entity_inflight_ids(db: Session, project_id: str) -> set[tuple[str, str]]:
    """项目内在途资产生成任务：{(类型, 实体 id)}。

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
    out: set[tuple[str, str]] = set()
    for (params,) in rows:
        if not isinstance(params, dict):
            continue
        for key, kind in (
            ("__character_id__", "character"),
            ("__scene_id__", "scene"),
            ("__prop_id__", "prop"),
        ):
            entity_id = params.get(key)
            if entity_id:
                out.add((kind, str(entity_id)))
    return out


def _check_one_asset(
    db: Session,
    *,
    asset_id: str | None,
    name: str,
    asset_type: str,
    entity_id: str | None,
    inflight: set[tuple[str, str]],
    missing_detail: str,
    invalid_detail: str,
    file_detail: str,
) -> GateItem | None:
    """校验单个必需资产：指针 → 版本有效性 → 文件存在。返回阻断项或 None。"""
    generating = (asset_type, entity_id or "") in inflight

    def _blocked(reason: str, detail: str) -> GateItem:
        # 有在途生成任务时统一归因为「生成中」：用户需要的是「等它跑完」，
        # 而不是「去重新生成一张」——后者会造成重复提交。
        if generating:
            return GateItem(asset_id, name, asset_type, "asset_generating", f"{detail}（生成中）", entity_id)
        return GateItem(asset_id, name, asset_type, reason, detail, entity_id)

    if not asset_id:
        return _blocked("asset_missing", missing_detail)
    a = db.get(Asset, asset_id)
    if a is None:
        return _blocked("asset_invalid", invalid_detail)
    if a.validity != "current":
        return _blocked("asset_invalid", f"{invalid_detail}（{a.validity}）")
    if not media_file_exists(a.file_path):
        return _blocked("asset_file_missing", f"{file_detail}：{a.file_path}")
    return None


def evaluate_assets_gate(db: Session, project_id: str) -> GateResult:
    """计算项目资产就绪状态（每次调用基于实时数据，不缓存结论）。

    判定顺序：
    1. 必需资产存在、版本有效、文件存在（缺失/失效/文件缺失 → ASSETS_INCOMPLETE）
    2. 阻断项中存在在途生成任务 → ASSETS_GENERATING（提示等待而非重新生成）
    3. 全部通过 → ASSETS_READY

    不再有第 4 步「人工审核状态」判定：阶段审核机制已取消，
    分镜图能否生成只取决于必需资产是否真实生成完成。
    """
    project = db.get(Project, project_id)
    if project is None:
        return GateResult(False, GATE_INCOMPLETE, "project_not_found", "项目不存在")

    inflight = _entity_inflight_ids(db, project_id)
    blockers: list[GateItem] = []

    characters = db.query(Character).filter(Character.project_id == project_id).all()
    scene_rows = db.query(Scene).filter(Scene.project_id == project_id).all()
    props = (
        db.query(Prop)
        .filter(Prop.project_id == project_id, Prop.anchor_required.is_(True))
        .all()
    )

    char_blockers = [
        b
        for c in characters
        if (
            b := _check_one_asset(
                db,
                asset_id=c.portrait_asset_id,
                name=c.name,
                asset_type="character",
                entity_id=c.id,
                inflight=inflight,
                missing_detail="角色缺少定妆图",
                invalid_detail="定妆图已失效",
                file_detail="定妆图文件缺失",
            )
        )
    ]
    scene_blockers = [
        b
        for sc in scene_rows
        if (
            b := _check_one_asset(
                db,
                asset_id=sc.baseline_asset_id,
                name=sc.title or sc.id,
                asset_type="scene",
                entity_id=sc.id,
                inflight=inflight,
                missing_detail="场景缺少基准图",
                invalid_detail="基准图已失效",
                file_detail="基准图文件缺失",
            )
        )
    ]
    prop_blockers = [
        b
        for p in props
        if (
            b := _check_one_asset(
                db,
                asset_id=p.anchor_asset_id,
                name=p.name,
                asset_type="prop",
                entity_id=p.id,
                inflight=inflight,
                missing_detail="必需道具缺少锚定图",
                invalid_detail="锚定图已失效",
                file_detail="锚定图文件缺失",
            )
        )
    ]

    summary = {
        "character": {"total": len(characters), "done": len(characters) - len(char_blockers)},
        "scene": {"total": len(scene_rows), "done": len(scene_rows) - len(scene_blockers)},
        "prop": {"total": len(props), "done": len(props) - len(prop_blockers)},
    }
    blockers = char_blockers + scene_blockers + prop_blockers

    if not blockers:
        return GateResult(True, GATE_READY, "", "", summary=summary)

    if all(b.reason == "asset_generating" for b in blockers):
        return GateResult(
            False,
            GATE_GENERATING,
            "assets_generating",
            "必需资产仍在生成中，请等待生成任务完成后再生成分镜图",
            blockers,
            summary,
        )

    names = "、".join(
        f"{ASSET_TYPE_LABELS.get(b.asset_type, b.asset_type)}:{b.name}" for b in blockers[:5]
    )
    more = f" 等 {len(blockers)} 项" if len(blockers) > 5 else ""
    return GateResult(
        False,
        GATE_INCOMPLETE,
        "assets_incomplete",
        f"必需资产图片尚未全部生成完成（{names}{more}），请先在资产库补齐",
        blockers,
        summary,
    )


def assert_assets_gate(db: Session, project_id: str) -> None:
    """门禁断言：未就绪时抛出结构化 AppError（含分项统计与阻断明细）。

    所有创建分镜图生成任务的入口（API / 批量 / Agent / 执行器）统一调用本函数，
    payload 携带 blockers 明细与 summary 统计。
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
        summary=result.summary,
    )
