"""镜头出场清单（ShotCast）：镜头级契约 shot_plan 的单一读取口径。

分镜图生成改造 Phase 2：compiler / resolver / 门禁 / 注入器统一读这一份
「谁在画面里 / 哪些道具可见」清单，不再各写一套启发式推断（P0-2/P0-4）。

规则：
- ``shot.shot_plan`` 存在 → 完全以契约为准（画内角色 / 画外声音 / 可见道具），
  再与 ``PropShotState`` 做保护性合并：existence ∈ not_introduced/destroyed/
  consumed 的道具即便被规划列出也剔除并告警；visibility ∈ offscreen/occluded
  的道具保留在清单但 reference_required=False（不注入参考图）；
- 无 ``shot_plan`` → 旧口径：角色走 shot_subject_characters（character_id ∪
  对白说话人 ∪ @引用 ∪ 结构化动作字段），道具走 shot.props ∪ @引用兜底 ∪
  PropShotState 有记录项。历史数据行为尽量不变；
- 对白说话人不再自动视为在场：仅契约路径生效（画外音不占身份槽位、不计入
  人数约束）；legacy 路径保持原行为（shot_subject_characters 仍含对白说话人）。
"""
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import Character, Prop, PropShotState, Scene, Shot

# 生命周期保护：这些 existence 状态的道具不应出现在画面里
_ABSENT_EXISTENCE = {"not_introduced", "destroyed", "consumed"}
# 可见性保护：这些 visibility 状态的道具不注入参考图
_HIDDEN_VISIBILITY = {"offscreen", "occluded"}


@dataclass
class CastCharacter:
    """画内角色：携带契约里的站位/姿态/朝向/视线/首帧动作。"""

    character: Character
    role: str = "secondary"
    position: dict | None = None
    posture: str | None = None
    facing: str | None = None
    gaze_target: dict | None = None
    action_at_start: str | None = None


@dataclass
class CastProp:
    """可见道具：携带交互方式/持有人/位置与生命周期状态快照。"""

    prop: Prop
    interaction: str = "none"
    holder_id: str | None = None
    position: str | None = None
    state: PropShotState | None = None
    reference_required: bool = True


@dataclass
class ShotCast:
    """镜头出场清单：四个消费方（compiler/resolver/门禁/注入器）的唯一口径。"""

    characters: list[CastCharacter] = field(default_factory=list)
    offscreen: list[Character] = field(default_factory=list)
    props: list[CastProp] = field(default_factory=list)
    source: str = "legacy"  # "shot_plan" | "legacy"
    warnings: list[str] = field(default_factory=list)

    @property
    def character_ids(self) -> list[str]:
        return [item.character.id for item in self.characters]

    @property
    def prop_ids(self) -> list[str]:
        return [item.prop.id for item in self.props]


def resolve_shot_cast(db: Session, shot: Shot) -> ShotCast:
    """解析镜头出场清单：有契约走契约，无契约走旧启发式兜底。"""
    plan = shot.shot_plan if isinstance(shot.shot_plan, dict) else None
    if plan is not None and isinstance(plan.get("on_screen_characters"), list):
        return _cast_from_plan(db, shot, plan)
    return _cast_legacy(db, shot)


def _prop_states(db: Session, shot: Shot) -> dict[str, PropShotState]:
    rows = db.query(PropShotState).filter(PropShotState.shot_id == shot.id).all()
    return {row.prop_id: row for row in rows}


def _structured_prop_names(shot: Shot) -> list[str]:
    """结构化 shot.props → 道具名列表（兼容 list/JSON 串/逗号分隔串）。"""
    raw = getattr(shot, "props", None)
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x or "").strip()]
    if isinstance(raw, str) and raw.strip():
        import json as _json
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x or "").strip()]
            if isinstance(parsed, str) and parsed.strip():
                return [parsed.strip()]
        except (ValueError, TypeError):
            return [x.strip() for x in raw.split(",") if x.strip()]
    return []


def _cast_from_plan(db: Session, shot: Shot, plan: dict) -> ShotCast:
    """契约路径：完全以 shot_plan 为准，与 PropShotState 做保护性合并。"""
    cast = ShotCast(source="shot_plan")
    scene = db.get(Scene, shot.scene_id)
    project_id = scene.project_id if scene is not None else None

    for item in plan.get("on_screen_characters") or []:
        if not isinstance(item, dict):
            continue
        character_id = item.get("character_id")
        character = db.get(Character, character_id) if isinstance(character_id, str) else None
        if character is None:
            cast.warnings.append(
                f"shot_plan.on_screen_characters 引用未知角色：{character_id}")
            continue
        position = item.get("position")
        gaze = item.get("gaze_target")
        cast.characters.append(CastCharacter(
            character=character,
            role=item.get("role") or "secondary",
            position=position if isinstance(position, dict) else None,
            posture=item.get("posture"),
            facing=item.get("facing"),
            gaze_target=gaze if isinstance(gaze, dict) else None,
            action_at_start=item.get("action_at_start"),
        ))

    for item in plan.get("offscreen_voices") or []:
        if not isinstance(item, dict):
            continue
        character_id = item.get("character_id")
        character = db.get(Character, character_id) if isinstance(character_id, str) else None
        if character is None:
            cast.warnings.append(
                f"shot_plan.offscreen_voices 引用未知角色：{character_id}")
            continue
        cast.offscreen.append(character)

    # 说话人归类兜底：有台词但既不在画内也不在画外 → 告警并按画外处理
    # （不占身份槽位、不计入人数约束；规划侧校验见 storyboard_plan）
    classified = {c.character.id for c in cast.characters} | {c.id for c in cast.offscreen}
    for seg in shot.dialogue_segments or []:
        if not isinstance(seg, dict):
            continue
        speaker_id = seg.get("speaker_id") or seg.get("character_id")
        speaker = None
        if speaker_id:
            speaker = db.get(Character, speaker_id)
        elif seg.get("speaker"):
            query = db.query(Character).filter(Character.name == seg["speaker"])
            if project_id:
                query = query.filter(Character.project_id == project_id)
            speaker = query.first()
        if speaker is not None and speaker.id not in classified:
            cast.warnings.append(
                f"说话人『{speaker.name}』未归类为画内或画外，按画外处理")
            classified.add(speaker.id)
            cast.offscreen.append(speaker)

    states = _prop_states(db, shot)
    for item in plan.get("visible_props") or []:
        if not isinstance(item, dict):
            continue
        prop_id = item.get("prop_id")
        prop = db.get(Prop, prop_id) if isinstance(prop_id, str) else None
        if prop is None:
            cast.warnings.append(f"shot_plan.visible_props 引用未知道具：{prop_id}")
            continue
        state = states.get(prop.id)
        if state is not None and state.existence_state in _ABSENT_EXISTENCE:
            cast.warnings.append(
                f"道具『{prop.name}』被规划列为可见，但生命周期状态为 "
                f"{state.existence_state}，已从画面剔除")
            continue
        reference_required = not (state is not None
                                  and state.visibility in _HIDDEN_VISIBILITY)
        cast.props.append(CastProp(
            prop=prop,
            interaction=item.get("interaction") or "none",
            holder_id=item.get("holder_character_id"),
            position=item.get("position"),
            state=state,
            reference_required=reference_required,
        ))
    return cast


def _cast_legacy(db: Session, shot: Shot) -> ShotCast:
    """旧口径：角色走 shot_subject_characters，道具走 shot.props ∪ @引用 ∪ 状态记录。"""
    from app.services.ref_inject import extract_refs
    from app.services.shot_refs import shot_subject_characters

    cast = ShotCast(source="legacy")
    for character in shot_subject_characters(db, shot):
        cast.characters.append(CastCharacter(
            character=character,
            role="primary" if character.id == shot.character_id else "secondary",
        ))

    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return cast
    project_id = scene.project_id
    structured = _structured_prop_names(shot)
    mentioned: list[str] = []
    for text in (shot.image_prompt or "", shot.video_prompt or ""):
        for name in extract_refs(text):
            if name not in mentioned:
                mentioned.append(name)
    names = set(structured) | set(mentioned)
    states = _prop_states(db, shot)
    rows = db.query(Prop).filter(Prop.project_id == project_id).all()
    for prop in rows:
        in_shot = prop.name in names or prop.id in states
        if not in_shot:
            continue
        state = states.get(prop.id)
        if state is not None and state.existence_state in _ABSENT_EXISTENCE:
            continue
        reference_required = not (state is not None
                                  and state.visibility in _HIDDEN_VISIBILITY)
        cast.props.append(CastProp(
            prop=prop,
            interaction=(state.interaction_state if state is not None else None) or "none",
            state=state,
            reference_required=reference_required,
        ))
    return cast
