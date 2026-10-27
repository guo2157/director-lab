"""对话内生成申请的镜头定位（生成能力接入对话）。

设计约束（AGENTS.md P0）：不猜用户意图对象。
- 只在消息明确给出镜头标识时定位：sht_ 前缀的镜头 ID，或『第 N 镜』
- 『第 N 镜』在项目内必须唯一命中；命中多个视为无法确定，交澄清而非挑一个
- 定位不到一律不创建申请，避免生成错目标造成算力与成本浪费
- 生成目标（关键帧/视频）由关键词判定，判定不出时默认关键帧（最常用且成本最低）
"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Scene, Shot

# 镜头 ID：与项目内 ID 规则一致（sht_ + 小写字母数字）
_SHOT_ID_RE = re.compile(r"\bsht_[a-z0-9]{4,}\b")
# 『第 3 镜』『第3个镜头』等常见表述
_SHOT_SEQ_RE = re.compile(r"第\s*(\d+)\s*(?:个)?\s*(?:镜|镜头)")

# 生成目标判定：先判视频（更具体），再判图片类，都未命中默认关键帧
_CLIP_HINTS = ("视频", "片段", "动态")
_KEYFRAME_HINTS = ("关键帧", "分镜图", "静图", "首帧", "图片", "出图")


def resolve_target(text: str) -> str:
    """从消息判定生成目标：clip / keyframe。判定不出时取成本最低的关键帧。"""
    if any(k in text for k in _CLIP_HINTS):
        return "clip"
    if any(k in text for k in _KEYFRAME_HINTS):
        return "keyframe"
    return "keyframe"


def resolve_shot(db: Session, project_id: str, text: str) -> tuple[Shot | None, str | None]:
    """定位消息所指镜头。

    返回 (shot, None) 表示唯一定位成功；
    返回 (None, reason) 表示无法定位，reason 取值：
    - shot_unspecified：消息未给出可识别的镜头标识
    - shot_not_found：给出的标识在库中不存在
    - shot_not_in_project：镜头存在但不属于当前项目
    - shot_ambiguous：『第 N 镜』在项目内命中多个，无法确定
    """
    matched = _SHOT_ID_RE.search(text or "")
    if matched:
        shot = db.get(Shot, matched.group(0))
        if shot is None:
            return None, "shot_not_found"
        scene = db.get(Scene, shot.scene_id)
        if scene is None or scene.project_id != project_id:
            return None, "shot_not_in_project"
        return shot, None

    seq_match = _SHOT_SEQ_RE.search(text or "")
    if seq_match:
        seq = int(seq_match.group(1))
        shots = list(
            db.scalars(
                select(Shot)
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.project_id == project_id, Shot.seq == seq)
            ).all()
        )
        if not shots:
            return None, "shot_not_found"
        if len(shots) > 1:
            return None, "shot_ambiguous"
        return shots[0], None

    return None, "shot_unspecified"
