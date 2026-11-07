"""Reference Resolver（参考图解析与绑定引擎）。

对应 PROP_BINDING_PLAN.md §3/§4：
- 语义需求（分镜涉及角色/场景/道具 + 生命周期状态）→ 资产库精确查询
- 组合参考图（character_prop_binding）优先、独立图回退、offscreen/absent 保护
- 输出 Reference Manifest（槽位、asset_id、选择原因、回退原因）

设计约束：
- 图片选择逻辑为纯代码，不写进 LLM Prompt
- 所有参考图通过 asset_id 精确查找，禁止按图片索引猜身份
- 不另建资产库：组合图是关系（CharacterPropBinding）的附属视觉参考
- 替换 shot_refs.inject_asset_refs 的状态驱动部分；启发式保留为降级路径
"""
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import Asset, Character, CharacterPropBinding, Prop, PropShotState, Scene, Shot


@dataclass
class SlotSelection:
    """一个参考槽位的选择结果。"""

    slot: str
    asset_id: str
    asset_type: str
    file_path: str | None
    source: str                     # portrait / anchor / binding:cpb_xxx / scene_anchor
    note: str | None = None
    replaces: list[str] = field(default_factory=list)


@dataclass
class FallbackRecord:
    """一条回退记录：为何未按首选方案选择。"""

    entity_id: str
    kind: str                       # prop / binding
    reason: str
    used: str


@dataclass
class ReferenceManifest:
    """Reference Manifest：本次生成的完整参考图清单与决策记录。"""

    shot_id: str
    slots: list[SlotSelection] = field(default_factory=list)
    fallbacks: list[FallbackRecord] = field(default_factory=list)
    lifecycle_snapshot_version: int = 0

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "shot_id": self.shot_id,
            "slots": [
                {
                    "slot": s.slot,
                    "asset_id": s.asset_id,
                    "asset_type": s.asset_type,
                    "file_path": s.file_path,
                    "source": s.source,
                    "note": s.note,
                    "replaces": s.replaces,
                }
                for s in self.slots
            ],
            "fallbacks": [
                {"entity_id": f.entity_id, "kind": f.kind,
                 "reason": f.reason, "used": f.used}
                for f in self.fallbacks
            ],
            "lifecycle_snapshot_version": self.lifecycle_snapshot_version,
        }


def _valid_asset(db: Session, asset_id: str | None) -> Asset | None:
    """取有效（current validity）资产；锚定缺失/失效返回 None。"""
    if not asset_id:
        return None
    a = db.get(Asset, asset_id)
    if a is None or (a.validity or "current") != "current":
        return None
    return a


def _event_active(db: Session, event_id: str | None) -> bool:
    """有效期边界事件是否有效：事件存在且未被 rejected/superseded。"""
    if event_id is None:
        return True
    from app.models import PropLifecycleEvent
    ev = db.get(PropLifecycleEvent, event_id)
    if ev is None:
        # 事件不存在（可能被清理）：边界不可考，关系不生效
        return False
    if ev.review_status == "rejected":
        return False
    if ev.supersedes_id is not None:
        return False
    return True


def _find_binding(db: Session, project_id: str, character_ids: list[str],
                  prop_id: str) -> CharacterPropBinding | None:
    """查找有效且已确认的绑定关系：角色在镜头中、review_status=confirmed、
    有效期边界事件均有效、组合图存在且有效。"""
    if not character_ids:
        return None
    rows = (
        db.query(CharacterPropBinding)
        .filter(
            CharacterPropBinding.project_id == project_id,
            CharacterPropBinding.character_id.in_(character_ids),
            CharacterPropBinding.prop_id == prop_id,
            CharacterPropBinding.review_status == "confirmed",
        )
        .all()
    )
    for b in rows:
        if not _event_active(db, b.valid_from_event):
            continue
        if b.valid_until_event and not _event_active(db, b.valid_until_event):
            continue
        if _valid_asset(db, b.anchor_asset_id) is None:
            continue
        return b
    return None

def resolve_shot_references(
    db: Session, shot_id: str, project_id: str, *, target: str = "keyframe"
) -> ReferenceManifest:
    """解析一个分镜的参考图清单（Reference Resolver 主入口，纯代码无 LLM）。

    流程（PROP_BINDING_PLAN.md §3.2）：
    1. 收集语义需求：镜头主角色、提示词 @引用实体、道具生命周期状态
    2. 逐实体解析资产：组合图优先 → 独立图回退 → offscreen/absent 保护
    3. 输出 ReferenceManifest（槽位、asset_id、来源、回退原因）
    """
    from app.services.ref_inject import extract_refs

    shot = db.get(Shot, shot_id)
    if shot is None:
        raise ValueError(f"shot_not_found: {shot_id}")
    scene = db.get(Scene, shot.scene_id)
    manifest = ReferenceManifest(shot_id=shot_id)

    # ---- 场景槽位 ----
    if scene is not None:
        scene_anchor = _valid_asset(db, getattr(scene, "baseline_asset_id", None))
        if scene_anchor is not None:
            manifest.slots.append(SlotSelection(
                slot="ref_scene", asset_id=scene_anchor.id, asset_type="scene",
                file_path=scene_anchor.file_path, source="scene_anchor",
                note=scene.title,
            ))

    # ---- 角色槽位 ----
    prompt_text = shot.image_prompt or ""
    mentioned = set(extract_refs(prompt_text))
    characters = (
        db.query(Character).filter(Character.project_id == project_id).all()
    )
    char_by_name = {c.name: c for c in characters}
    selected_chars: list[tuple[Character, Asset]] = []
    used_slots = 0
    for c in characters:
        asset = _valid_asset(db, c.portrait_asset_id)
        if asset is None:
            continue
        in_shot = (
            shot.character_id == c.id
            or c.name in mentioned
            or c.name in (shot.dialogue or "")
        )
        if not in_shot:
            continue
        selected_chars.append((c, asset))
    # 主角色占 ref_identity，其余依次占 ref_character_2/3/...
    primary = next(
        ((c, a) for c, a in selected_chars if c.id == shot.character_id),
        selected_chars[0] if selected_chars else None,
    )
    others = [(c, a) for c, a in selected_chars if (c, a) != primary]
    if primary is not None:
        c, a = primary
        manifest.slots.append(SlotSelection(
            slot="ref_identity", asset_id=a.id, asset_type="character",
            file_path=a.file_path, source="portrait", note=c.name,
        ))
        used_slots += 1
    for i, (c, a) in enumerate(others, start=2):
        if i > 6:  # ref_character_2..6 上限（槽位 9 留 3 个给场景/道具/连续性）
            manifest.fallbacks.append(FallbackRecord(
                entity_id=c.id, kind="character",
                reason="slot_limit", used="skip"))
            continue
        manifest.slots.append(SlotSelection(
            slot=f"ref_character_{i}", asset_id=a.id, asset_type="character",
            file_path=a.file_path, source="portrait", note=c.name,
        ))
        used_slots += 1

    # ---- 道具槽位（生命周期状态驱动 + 组合图优先）----
    states = (
        db.query(PropShotState)
        .filter(
            PropShotState.project_id == project_id,
            PropShotState.shot_id == shot_id,
        )
        .all()
    )
    prop_ids_with_state = {st.prop_id for st in states}
    props = (
        db.query(Prop).filter(Prop.project_id == project_id).all()
    )
    props_to_resolve: list[tuple[Prop, PropShotState | None]] = []
    for p in props:
        st = next((s for s in states if s.prop_id == p.id), None)
        in_shot = p.name in mentioned or p.id in prop_ids_with_state
        if in_shot:
            props_to_resolve.append((p, st))

    char_ids_in_shot = [c.id for c, _ in selected_chars]
    prop_slot_seq = 0
    for p, st in props_to_resolve:
        existence = st.existence_state if st else "unknown"
        visibility = st.visibility if st else "unknown"
        interaction = st.interaction_state if st else "unassigned"

        # 保护规则：不存在 → 不注入并禁止；画外/遮挡 → 不强制入画
        if existence in ("not_introduced", "destroyed", "consumed"):
            manifest.fallbacks.append(FallbackRecord(
                entity_id=p.id, kind="prop",
                reason=f"prop_{existence}", used="negative_constraint"))
            continue
        if visibility in ("offscreen", "occluded"):
            manifest.fallbacks.append(FallbackRecord(
                entity_id=p.id, kind="prop",
                reason=f"prop_{visibility}", used="not_injected"))
            continue

        # 先探测绑定关系是否存在（用于回退原因记录，与组合图有效性分开判定）
        binding_probe = (
            db.query(CharacterPropBinding)
            .filter(
                CharacterPropBinding.project_id == project_id,
                CharacterPropBinding.character_id.in_(char_ids_in_shot),
                CharacterPropBinding.prop_id == p.id,
                CharacterPropBinding.review_status == "confirmed",
            )
            .first()
        )

        prop_asset = _valid_asset(db, p.anchor_asset_id)
        if prop_asset is None:
            manifest.fallbacks.append(FallbackRecord(
                entity_id=p.id, kind="prop",
                reason="prop_anchor_missing", used="skip"))
            continue

        # 组合图优先：镜头中出现的关系角色 + confirmed 绑定 + 有效组合图
        binding = _find_binding(db, project_id, char_ids_in_shot, p.id)
        if binding is None and binding_probe is not None:
            # 有 confirmed 绑定但组合图缺失/失效/边界失效：记录回退原因
            manifest.fallbacks.append(FallbackRecord(
                entity_id=binding_probe.id, kind="binding",
                reason="no_valid_combo_image", used="independent_refs"))
        if binding is not None:
            combo = _valid_asset(db, binding.anchor_asset_id)
            if combo is not None:
                # shared 关系：镜头必须包含全部持有角色才可用组合图
                ok_shared = True
                if binding.relation_type == "shared":
                    from app.models import CharacterPropBinding as _CPB
                    all_rows = db.query(_CPB).filter(
                        _CPB.project_id == project_id,
                        _CPB.prop_id == p.id,
                        _CPB.review_status == "confirmed",
                    ).all()
                    ok_shared = all(r.character_id in char_ids_in_shot for r in all_rows)
                if ok_shared:
                    # 组合图替换：绑定角色的 portrait 槽位 + 该道具独立槽位
                    replaced: list[str] = []
                    holder_slot = next(
                        (s for s in manifest.slots
                         if s.asset_id == _portrait_of(db, binding.character_id)),
                        None,
                    )
                    if holder_slot is not None:
                        replaced.append(holder_slot.asset_id)
                        manifest.slots.remove(holder_slot)
                    # 移除已加入的本道具独立槽位（若有）
                    dup = next(
                        (s for s in manifest.slots if s.asset_id == prop_asset.id),
                        None,
                    )
                    if dup is not None:
                        replaced.append(dup.asset_id)
                        manifest.slots.remove(dup)
                    manifest.slots.append(SlotSelection(
                        slot="ref_prop", asset_id=combo.id,
                        asset_type="character_prop_binding",
                        file_path=combo.file_path,
                        source=f"binding:{binding.id}",
                        note=f"{binding.relation_type} 组合图",
                        replaces=replaced,
                    ))
                    continue
                manifest.fallbacks.append(FallbackRecord(
                    entity_id=binding.id, kind="binding",
                    reason="shared_binding_incomplete_cast", used="independent_refs"))

        # placed（独立放置）：场景图 + 道具独立图；不使用携带类组合图
        if interaction == "placed" and binding is not None:
            manifest.fallbacks.append(FallbackRecord(
                entity_id=binding.id, kind="binding",
                reason="prop_placed_binding_disabled", used="independent_refs"))

        # 独立道具图回退
        prop_slot_seq += 1
        slot = "ref_prop" if prop_slot_seq == 1 else f"ref_prop_{prop_slot_seq}"
        manifest.slots.append(SlotSelection(
            slot=slot, asset_id=prop_asset.id, asset_type="prop",
            file_path=prop_asset.file_path, source="anchor", note=p.name,
        ))

    manifest.lifecycle_snapshot_version = max(
        (st.state_version for st in states), default=0)
    return manifest


def _portrait_of(db: Session, character_id: str) -> str | None:
    c = db.get(Character, character_id)
    return c.portrait_asset_id if c else None


def manifest_to_media(manifest: ReferenceManifest) -> dict[str, str]:
    """把 Manifest 转为语义槽位映射 {slot: file_path}（兼容 build_media_params 下游）。"""
    return {
        s.slot: s.file_path
        for s in manifest.slots if s.file_path
    }
