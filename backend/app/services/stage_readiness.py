"""阶段就绪检查：资产库 / 分镜图 / 分镜视频 / 合成导出四个阶段共用的权威判定。

设计背景（2026-11 流程简化）：
阶段之间的推进不再依赖人工审核状态，改为「生成是否真的完成」。前端在跳转
按钮上弹出确认弹窗，后端负责权威数据校验——不能只检查数据库里有没有
asset_id，必须检查：

1. 该镜头存在**当前有效版本**（asset.validity == "current"）的产物；
2. 产物的来源任务确实成功结束（job.status == "succeeded"）；
3. 产物文件在磁盘上真实存在（文件被删/被清理不算完成）；
4. 生成失败且没有有效当前版本的镜头算未完成；
5. 正在生成且尚无有效版本的镜头算未完成（有旧有效版本则按版本有效性放行）。

检查范围：项目内**全部集**的全部镜头。多集项目不能只查第一集，
否则一集出完就能把整个项目放行到合成。

与 assets_gate 的分工：
- assets_gate 管「公共资产（角色/场景/道具）是否就绪」→ 能否生成分镜图；
- stage_readiness 管「镜头级产物是否就绪」→ 能否进入下一阶段；
- 资产库阶段（stage=assets）不重复实现资产规则，直接调用
  assets_gate.evaluate_assets_gate 并映射成同一返回结构，避免两处口径漂移。
两者都是后端权威判定，前端传入的标志不可绕过。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import Asset, Job, Project, Scene, Shot
from app.services.assets_gate import (
    ASSET_TYPE_LABELS,
    evaluate_assets_gate,
    media_file_exists,
)

# 阶段标识（与前端 stage-readiness 路径参数一致）
STAGE_ASSETS = "assets"
STAGE_STORYBOARD_IMAGES = "storyboard_images"
STAGE_STORYBOARD_VIDEOS = "storyboard_videos"
STAGE_COMPOSE = "compose"

STAGE_LABELS = {
    STAGE_ASSETS: "资产图",
    STAGE_STORYBOARD_IMAGES: "分镜图",
    STAGE_STORYBOARD_VIDEOS: "分镜视频",
    STAGE_COMPOSE: "分镜视频",
}

# 阶段 → 该阶段要求的镜头产物类型
STAGE_ASSET_TYPE = {
    STAGE_STORYBOARD_IMAGES: "keyframe",
    STAGE_STORYBOARD_VIDEOS: "clip",
    STAGE_COMPOSE: "clip",
}

# 阶段 → 产物生成任务类型
STAGE_JOB_TYPE = {
    STAGE_STORYBOARD_IMAGES: "generate_keyframe",
    STAGE_STORYBOARD_VIDEOS: "generate_clip",
    STAGE_COMPOSE: "generate_clip",
}

_INFLIGHT_STATUSES = ("queued", "running", "chained_wait", "submission_unknown")
_FAILED_STATUSES = ("failed", "lost", "cancelled")


@dataclass
class StageBlocker:
    """单个镜头的阻断明细。"""

    shot_id: str
    episode: int
    scene_title: str
    seq: int
    name: str  # 展示名：第 N 集 第 M 镜
    reason: str  # shot_missing_asset / shot_generating / shot_failed / shot_file_missing / shot_asset_invalid
    detail: str = ""
    # 仅资产类阻断会填充（资产库阶段）；镜头类阻断保持 None
    asset_id: str | None = None
    entity_id: str | None = None
    asset_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "shot_id": self.shot_id,
            "episode": self.episode,
            "scene_title": self.scene_title,
            "seq": self.seq,
            "name": self.name,
            "reason": self.reason,
            "detail": self.detail,
            "asset_id": self.asset_id,
            "entity_id": self.entity_id,
            "asset_type": self.asset_type,
            "asset_type_label": (
                ASSET_TYPE_LABELS.get(self.asset_type, self.asset_type)
                if self.asset_type
                else None
            ),
        }


@dataclass
class StageReadiness:
    """阶段就绪结果：ready + 进度统计 + 阻断明细。"""

    stage: str
    ready: bool
    total: int
    done: int
    failed: int = 0
    generating: int = 0
    missing: list[StageBlocker] = field(default_factory=list)
    blockers: list[StageBlocker] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "ready": self.ready,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "generating": self.generating,
            "missing": [b.to_dict() for b in self.missing],
            "blockers": [b.to_dict() for b in self.blockers],
            "message": self.message,
        }


def _ordered_shots(db: Session, project_id: str) -> list[tuple[Shot, str]]:
    """项目内全部集的镜头（按集 → 场景 seq → 镜头 seq 排序），附带场景标题。"""
    rows = (
        db.query(Shot, Scene.title)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .order_by(Shot.episode, Scene.seq, Shot.seq)
        .all()
    )
    return [(shot, title or "") for shot, title in rows]


def _valid_asset_by_shot(
    db: Session, shot_ids: list[str], asset_type: str
) -> dict[str, Asset]:
    """镜头 → 当前有效且来源任务成功的产物（同镜多版本取 version 最大者）。"""
    if not shot_ids:
        return {}
    rows = (
        db.query(Asset, Job.shot_id)
        .join(Job, Job.id == Asset.source_job_id)
        .filter(
            Job.shot_id.in_(shot_ids),
            Job.status == "succeeded",
            Asset.asset_type == asset_type,
            Asset.validity == "current",
        )
        .order_by(Asset.version.asc())
        .all()
    )
    out: dict[str, Asset] = {}
    for asset, shot_id in rows:
        if shot_id:
            out[shot_id] = asset  # 升序遍历 → 最后写入的是最高版本
    return out


def _job_status_by_shot(
    db: Session, project_id: str, job_type: str, shot_ids: Iterable[str]
) -> tuple[set[str], set[str]]:
    """返回 (在途镜头集合, 最近失败镜头集合)。"""
    ids = list(shot_ids)
    if not ids:
        return set(), set()
    rows = (
        db.query(Job.shot_id, Job.status)
        .filter(
            Job.project_id == project_id,
            Job.job_type == job_type,
            Job.shot_id.in_(ids),
        )
        .all()
    )
    inflight: set[str] = set()
    failed: set[str] = set()
    for shot_id, status in rows:
        if not shot_id:
            continue
        if status in _INFLIGHT_STATUSES:
            inflight.add(shot_id)
        elif status in _FAILED_STATUSES:
            failed.add(shot_id)
    # 已有在途任务时，历史失败不再作为阻断理由（正在重试）
    return inflight, failed - inflight


def check_stage_readiness(db: Session, project_id: str, stage: str) -> StageReadiness:
    """计算某阶段的就绪状态（每次调用基于实时数据，不缓存结论）。"""
    if stage == STAGE_ASSETS:
        # 资产库阶段复用 assets_gate 权威判定，不重复实现资产校验规则
        return check_assets_readiness(db, project_id)
    if stage not in STAGE_ASSET_TYPE:
        raise ValueError(f"未知阶段：{stage}")
    asset_type = STAGE_ASSET_TYPE[stage]
    job_type = STAGE_JOB_TYPE[stage]
    label = STAGE_LABELS[stage]

    project = db.get(Project, project_id)
    if project is None:
        return StageReadiness(stage, False, 0, 0, message="项目不存在")

    shots = _ordered_shots(db, project_id)
    total = len(shots)
    if total == 0:
        return StageReadiness(
            stage,
            False,
            0,
            0,
            message=f"项目还没有镜头，无法进入下一阶段；请先在解析页完成解析",
        )

    shot_ids = [s.id for s, _ in shots]
    valid_assets = _valid_asset_by_shot(db, shot_ids, asset_type)
    inflight, failed = _job_status_by_shot(db, project_id, job_type, shot_ids)

    blockers: list[StageBlocker] = []
    done = 0
    for shot, scene_title in shots:
        name = f"第 {shot.episode} 集 第 {shot.seq} 镜"
        asset = valid_assets.get(shot.id)
        if asset is not None:
            if media_file_exists(asset.file_path):
                done += 1
                continue
            # 记录存在但文件不在：不算完成（文件被清理/迁移丢失）
            blockers.append(
                StageBlocker(
                    shot.id,
                    shot.episode,
                    scene_title,
                    shot.seq,
                    name,
                    "shot_file_missing",
                    f"{label}文件不存在：{asset.file_path}",
                )
            )
            continue
        if shot.id in inflight:
            blockers.append(
                StageBlocker(
                    shot.id, shot.episode, scene_title, shot.seq, name,
                    "shot_generating", f"{label}正在生成中",
                )
            )
        elif shot.id in failed:
            blockers.append(
                StageBlocker(
                    shot.id, shot.episode, scene_title, shot.seq, name,
                    "shot_failed", f"{label}生成失败且没有可用的当前版本",
                )
            )
        else:
            blockers.append(
                StageBlocker(
                    shot.id, shot.episode, scene_title, shot.seq, name,
                    "shot_missing_asset", f"尚未生成{label}",
                )
            )

    generating = sum(1 for b in blockers if b.reason == "shot_generating")
    failed_count = sum(1 for b in blockers if b.reason in ("shot_failed", "shot_file_missing"))
    ready = done == total
    message = ""
    if not ready:
        message = f"{label}尚未全部生成完成（{done}/{total}），请完成所有{label}生成后再进入下一阶段"
    return StageReadiness(
        stage=stage,
        ready=ready,
        total=total,
        done=done,
        failed=failed_count,
        generating=generating,
        missing=blockers,
        blockers=blockers,
        message=message,
    )


def check_storyboard_images_ready(db: Session, project_id: str) -> StageReadiness:
    """分镜图阶段就绪：全部必要镜头的当前有效分镜图已生成且文件存在。"""
    return check_stage_readiness(db, project_id, STAGE_STORYBOARD_IMAGES)


def check_storyboard_videos_ready(db: Session, project_id: str) -> StageReadiness:
    """分镜视频阶段就绪：全部必要镜头的当前有效视频已生成且文件存在。"""
    return check_stage_readiness(db, project_id, STAGE_STORYBOARD_VIDEOS)


def check_assets_readiness(db: Session, project_id: str) -> StageReadiness:
    """资产库阶段就绪：必需角色/场景/必需道具的图片是否真实生成完成。

    直接复用 assets_gate.evaluate_assets_gate（指针有效 + 版本 current +
    文件真实存在 + 无在途生成任务），只把结果映射成统一结构，
    这样「资产库 → 分镜图」的跳转判定与后端生成门禁口径完全一致，
    不会出现「前端放行但生成报错」或「前端拦死但后端其实可生成」。
    """
    gate = evaluate_assets_gate(db, project_id)
    blockers = [
        StageBlocker(
            shot_id="",
            episode=0,
            scene_title="",
            seq=0,
            name=b.name,
            reason=b.reason,
            detail=b.detail,
            asset_id=b.asset_id,
            entity_id=b.entity_id,
            asset_type=b.asset_type,
        )
        for b in gate.blockers
    ]
    return StageReadiness(
        stage=STAGE_ASSETS,
        ready=gate.ready,
        total=gate.total,
        done=gate.done,
        failed=sum(
            1 for b in blockers if b.reason in ("asset_invalid", "asset_file_missing")
        ),
        generating=sum(1 for b in blockers if b.reason == "asset_generating"),
        missing=blockers,
        blockers=blockers,
        message="" if gate.ready else gate.message,
    )


def check_assets_ready(db: Session, project_id: str) -> StageReadiness:
    """资产库阶段就绪（命名与其它阶段保持一致）。"""
    return check_assets_readiness(db, project_id)
