"""分镜规划契约校验、兼容 Adapter 预览与既有 Shot 对比（Phase 2 切片）。

设计约定：
- 校验与转换默认只读：``validate_storyboard_plan`` 不写 Shot、不改存量数据。
- 写入需显式调用 ``apply_storyboard_plan``，且只在规划零错误时执行：
  - 只写规划显式提供的字段，缺省字段保留既有值（尤其 image_prompt，避免
    清空生产层已编译的提示词）；
  - 已确认锁定镜头（keyframe_confirmed）的 seq/duration/shot_kind 不被覆盖，
    只报告并跳过；
  - 不删除规划未包含的旧镜头，只返回 orphan_shot_ids 供人工处理；
  - 不重建资产计划、不调用生成链路。
- 规划层只决定“怎么拍”，不分配参考槽位、不编译生产 Prompt；
  ``image_prompt`` 仅在规划文档显式提供时透传，缺失时留给 PromptCompiler。
- 每个镜头必须带原文区块证据，且资产引用使用真实 ID，不以名称作为唯一引用。
- 新结构通过 Adapter 映射为现有 Shot 字段，保持当前生产流程兼容。
"""
from itertools import pairwise

from sqlalchemy.orm import Session

from app.models import Character, Prop, Scene, ScriptVersion, Shot
from app.models.enums import SHOT_KIND
from app.services.script_analysis import build_structure, scene_key
from app.services.source_index import build_source_index

PLAN_ALGORITHM = "storyboard-plan-adapter-v1"
PLAN_APPLY_ALGORITHM = "storyboard-plan-apply-v1"
MAX_PLAN_SHOTS = 500
MAX_DURATION_SECONDS = 60
MAX_SHOT_ID_LENGTH = 32

# 规划字段 → Shot 列。只有规划文档显式出现的键才写入，避免误清空既有值。
_WRITE_FIELD_MAP = (
    ("sequence", "seq"),
    ("duration", "duration"),
    ("shot_kind", "shot_kind"),
    ("shot_type", "shot_type"),
    ("camera_angle", "camera_angle"),
    ("camera_movement", "camera_movement"),
    ("dialogue", "dialogue"),
    ("shot_purpose", "shot_purpose"),
    ("emotion", "emotion"),
    ("sound_notes", "soundscape"),
    ("start_state", "start_state"),
    ("end_state", "end_state"),
)


def _issue(issues: list[dict], level: str, code: str, path: str,
           message: str, **extra) -> None:
    issues.append({"level": level, "code": code, "path": path,
                   "message": message, **extra})


def _invalid_items(shots: object, issues: list[dict]) -> list | None:
    if not isinstance(shots, list):
        _issue(issues, "error", "invalid_value", "plan.shots",
               "shots 必须是数组")
        return None
    if len(shots) > MAX_PLAN_SHOTS:
        _issue(issues, "error", "too_many_items", "plan.shots",
               f"镜头数量超过上限 {MAX_PLAN_SHOTS}")
        return None
    return shots


def _validate_assets(issues: list[dict], value: object, path: str,
                     entity_map: dict[str, object], label: str) -> list[str]:
    """校验资产 ID 存在、属于当前项目且类型正确；返回合法 ID 列表。"""
    if value is None:
        return []
    if not isinstance(value, list):
        _issue(issues, "error", "invalid_value", path, f"{label} 必须是数组")
        return []
    valid: list[str] = []
    for position, asset_id in enumerate(value):
        item_path = f"{path}[{position}]"
        if not isinstance(asset_id, str) or not asset_id.strip():
            _issue(issues, "error", "invalid_value", item_path,
                   f"{label} ID 必须是非空字符串")
            continue
        if asset_id not in entity_map:
            _issue(issues, "error", "asset_not_found", item_path,
                   f"{label} ID 不存在或不属于当前项目：{asset_id}")
            continue
        valid.append(asset_id)
    return valid


def _validate_evidence(issues: list[dict], value: object, path: str,
                       block_ids: set[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        _issue(issues, "error", "missing_field", path,
               "镜头缺少原文证据 source_block_ids")
        return []
    valid: list[str] = []
    for position, block_id in enumerate(value):
        if not isinstance(block_id, str) or not block_id:
            _issue(issues, "error", "invalid_value", f"{path}[{position}]",
                   "source_block_id 必须是非空字符串")
        elif block_id not in block_ids:
            _issue(issues, "error", "source_block_not_found", f"{path}[{position}]",
                   "原文区块不存在或不属于当前剧本版本",
                   source_block_id=block_id)
        else:
            valid.append(block_id)
    return valid


def _check_sequences(issues: list[dict], shots: list[dict]) -> None:
    by_scene: dict[str, list[tuple[int, str]]] = {}
    for shot in shots:
        scene_id = shot.get("scene_id")
        sequence = shot.get("sequence")
        if isinstance(scene_id, str) and isinstance(sequence, int):
            by_scene.setdefault(scene_id, []).append((sequence, shot.get("shot_id", "")))
    for scene_id, entries in by_scene.items():
        ordered = sorted(entries)
        expected = list(range(1, len(ordered) + 1))
        if [sequence for sequence, _ in ordered] != expected:
            _issue(issues, "warning", "sequence_not_contiguous", f"scene:{scene_id}",
                   "同一场景内镜头序号必须从 1 连续递增",
                   related_ids=[shot_id for _, shot_id in ordered])


def _coverage_issues(issues: list[dict], structure: dict, planned_scene_keys: set[str],
                     covered_blocks: set[str]) -> dict:
    scene_entries = [
        {"scene_id": scene["scene_id"], "title": scene["title"],
         "key": scene_key(scene.get("location") or scene["title"])}
        for scene in structure["scenes"]
    ]
    uncovered_scenes = [
        entry["scene_id"] for entry in scene_entries if entry["key"] not in planned_scene_keys
    ]
    if uncovered_scenes:
        _issue(issues, "warning", "scene_uncovered", "coverage.scenes",
               f"{len(uncovered_scenes)} 个原文场景没有任何规划镜头",
               related_ids=uncovered_scenes)

    by_kind: dict[str, dict] = {}
    for beat in structure["beats"]:
        block_id = beat["source_block_ids"][0]
        bucket = by_kind.setdefault(beat["kind"], {"total": 0, "covered": 0,
                                                   "uncovered_block_ids": []})
        bucket["total"] += 1
        if block_id in covered_blocks:
            bucket["covered"] += 1
        else:
            bucket["uncovered_block_ids"].append(block_id)
    for kind, label, code in (
        ("dialogue", "对白", "dialogue_not_covered"),
        ("narration", "旁白", "narration_not_covered"),
    ):
        bucket = by_kind.get(kind)
        if bucket and bucket["uncovered_block_ids"]:
            _issue(issues, "warning", code, f"coverage.{kind}",
                   f"{len(bucket['uncovered_block_ids'])} 个{label}区块未被任何镜头覆盖",
                   source_block_ids=bucket["uncovered_block_ids"])
    total = sum(bucket["total"] for bucket in by_kind.values())
    covered = sum(bucket["covered"] for bucket in by_kind.values())
    return {
        "scenes": {
            "total": len(scene_entries),
            "covered": len(scene_entries) - len(uncovered_scenes),
            "uncovered_scene_ids": uncovered_scenes,
        },
        "beats": {
            "total": total,
            "covered": covered,
            "uncovered_block_ids": [
                block_id for bucket in by_kind.values()
                for block_id in bucket["uncovered_block_ids"]
            ],
            "by_kind": by_kind,
        },
    }


def _scene_order_issues(issues: list[dict], ordered_shot_scene_keys: list[str],
                        structure: dict) -> None:
    order = {
        scene_key(scene.get("location") or scene["title"]): position
        for position, scene in enumerate(structure["scenes"])
    }
    seen: list[int] = []
    for key in ordered_shot_scene_keys:
        if key in order:
            seen.append(order[key])
    if any(current < previous for previous, current in pairwise(seen)):
        _issue(issues, "warning", "scene_order_non_monotonic", "plan.shots",
               "镜头顺序出现场景回跳，请确认叙事顺序是否有意交叉剪辑")


def _existing_shots(db: Session, project_id: str) -> dict[str, list[Shot]]:
    rows = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .order_by(Shot.scene_id, Shot.seq)
        .all()
    )
    grouped: dict[str, list[Shot]] = {}
    for shot in rows:
        grouped.setdefault(shot.scene_id, []).append(shot)
    return grouped


def _compare_existing(issues: list[dict], plan_shots: list[dict],
                      adapted: list[dict], existing: dict[str, list[Shot]]) -> dict:
    existing_by_id = {
        shot.id: shot for shots in existing.values() for shot in shots
    }
    occupied = {
        (shot.scene_id, shot.seq) for shots in existing.values() for shot in shots
    }
    reused_ids: list[str] = []
    sequence_conflicts: list[dict] = []
    locked_conflicts: list[dict] = []
    for adapted_shot in adapted:
        shot_id = adapted_shot.get("id")
        scene_id = adapted_shot.get("scene_id")
        sequence = adapted_shot.get("seq")
        if shot_id in existing_by_id:
            reused_ids.append(shot_id)
            previous = existing_by_id[shot_id]
            if previous.keyframe_confirmed:
                changed = [
                    field for field in ("seq", "duration", "shot_kind")
                    if getattr(previous, field) != adapted_shot.get(field)
                ]
                if changed:
                    locked_conflicts.append({"shot_id": shot_id, "fields": changed})
        if (scene_id, sequence) in occupied:
            sequence_conflicts.append({"scene_id": scene_id, "seq": sequence,
                                       "shot_id": shot_id})
    if sequence_conflicts:
        _issue(issues, "warning", "sequence_conflict", "comparison.sequences",
               f"{len(sequence_conflicts)} 个镜头序号已被既有镜头占用",
               related_ids=[item["shot_id"] for item in sequence_conflicts])
    if locked_conflicts:
        _issue(issues, "warning", "locked_shot_modified", "comparison.locked",
               "规划将改动已确认锁定的镜头字段，必须人工确认后才可覆盖",
               related_ids=[item["shot_id"] for item in locked_conflicts])
    reused_plan_shot_ids = {shot.get("shot_id") for shot in plan_shots}
    existing_ids = set(existing_by_id)
    return {
        "existing_shot_count": sum(len(shots) for shots in existing.values()),
        "existing_locked_shot_ids": sorted(
            shot_id for shot_id, shot in existing_by_id.items() if shot.keyframe_confirmed
        ),
        "reused_shot_ids": sorted(set(reused_ids)),
        "removed_shot_ids": sorted(existing_ids - reused_plan_shot_ids),
        "sequence_conflicts": sequence_conflicts,
        "locked_conflicts": locked_conflicts,
        "field_changes": _field_changes(plan_shots, adapted, existing_by_id),
        "duration_total_seconds": sum(
            int(shot.get("duration") or 0) for shot in adapted
        ),
    }


def _field_changes(plan_shots: list[dict], adapted: list[dict],
                   existing_by_id: dict[str, Shot]) -> list[dict]:
    """写入前的逐镜头字段差异：与 ``apply`` 实际写入口径保持一致。

    - 只比较规划显式提供的字段（``_write_fields`` 决定），未提供的字段不会出现在差异里；
    - ``will_skip`` 表示该镜头因已锁定且关键字段（seq/duration/shot_kind）变化，
      写入时会被整体跳过，与 ``skipped_locked`` 判定一致；
    - 只读：不写库。
    """
    plan_by_id = {
        shot.get("shot_id"): shot for shot in plan_shots if isinstance(shot, dict)
    }
    changes: list[dict] = []
    for adapted_shot in adapted:
        shot_id = adapted_shot.get("id")
        previous = existing_by_id.get(shot_id)
        plan_shot = plan_by_id.get(shot_id)
        if previous is None or plan_shot is None:
            continue
        fields = _write_fields(plan_shot, adapted_shot)
        diffs = [
            {"field": column, "from": getattr(previous, column), "to": value}
            for column, value in fields.items()
            if getattr(previous, column) != value
        ]
        if not diffs:
            continue
        will_skip = bool(previous.keyframe_confirmed) and any(
            diff["field"] in ("seq", "duration", "shot_kind") for diff in diffs
        )
        changes.append({"shot_id": shot_id, "will_skip": will_skip, "fields": diffs})
    return changes


_SHOT_PLAN_OFFSCREEN_KINDS = ("voice_over", "offscreen_dialogue", "mentioned")
_SHOT_PLAN_PROP_INTERACTIONS = ("held", "carried", "worn", "placed", "none")
# 规划文档中属于镜头级契约的六个可选字段：任一出现即生成 shot_plan
_SHOT_PLAN_CONTRACT_KEYS = (
    "on_screen_character_ids", "offscreen_voices", "visible_props",
    "blocking", "composition", "keyframe_state",
)


def _validate_offscreen_voices(issues: list[dict], value: object, path: str,
                               characters: dict[str, Character]) -> tuple[list[str], list[dict]]:
    """校验画外声音数组；返回 (角色 ID 列表, 清洗后的契约项)。"""
    if value is None:
        return [], []
    if not isinstance(value, list):
        _issue(issues, "error", "invalid_value", path, "offscreen_voices 必须是数组")
        return [], []
    ids: list[str] = []
    items: list[dict] = []
    for position, item in enumerate(value):
        item_path = f"{path}[{position}]"
        if not isinstance(item, dict):
            _issue(issues, "error", "invalid_value", item_path, "画外声音项必须是对象")
            continue
        character_id = item.get("character_id")
        if not isinstance(character_id, str) or not character_id.strip():
            _issue(issues, "error", "missing_field", f"{item_path}.character_id",
                   "画外声音缺少 character_id")
            continue
        if character_id not in characters:
            _issue(issues, "error", "asset_not_found", f"{item_path}.character_id",
                   f"角色 ID 不存在或不属于当前项目：{character_id}")
            continue
        kind = item.get("kind") or "voice_over"
        if kind not in _SHOT_PLAN_OFFSCREEN_KINDS:
            _issue(issues, "error", "invalid_value", f"{item_path}.kind",
                   f"kind 必须是 {_SHOT_PLAN_OFFSCREEN_KINDS} 之一")
            continue
        ids.append(character_id)
        items.append({"character_id": character_id, "kind": kind})
    return ids, items


def _validate_visible_props(issues: list[dict], value: object, path: str,
                             props: dict[str, Prop],
                             characters: dict[str, Character]) -> tuple[list[str], list[dict]]:
    """校验可见道具数组；返回 (道具 ID 列表, 清洗后的契约项)。"""
    if value is None:
        return [], []
    if not isinstance(value, list):
        _issue(issues, "error", "invalid_value", path, "visible_props 必须是数组")
        return [], []
    ids: list[str] = []
    items: list[dict] = []
    for position, item in enumerate(value):
        item_path = f"{path}[{position}]"
        if not isinstance(item, dict):
            _issue(issues, "error", "invalid_value", item_path, "可见道具项必须是对象")
            continue
        prop_id = item.get("prop_id")
        if not isinstance(prop_id, str) or not prop_id.strip():
            _issue(issues, "error", "missing_field", f"{item_path}.prop_id",
                   "可见道具缺少 prop_id")
            continue
        if prop_id not in props:
            _issue(issues, "error", "asset_not_found", f"{item_path}.prop_id",
                   f"道具 ID 不存在或不属于当前项目：{prop_id}")
            continue
        holder = item.get("holder_character_id")
        if holder is not None and holder not in characters:
            _issue(issues, "error", "asset_not_found", f"{item_path}.holder_character_id",
                   f"持有角色 ID 不存在或不属于当前项目：{holder}")
            continue
        interaction = item.get("interaction") or "none"
        if interaction not in _SHOT_PLAN_PROP_INTERACTIONS:
            _issue(issues, "error", "invalid_value", f"{item_path}.interaction",
                   f"interaction 必须是 {_SHOT_PLAN_PROP_INTERACTIONS} 之一")
            continue
        ids.append(prop_id)
        items.append({
            "prop_id": prop_id,
            "holder_character_id": holder,
            "interaction": interaction,
            "position": item.get("position"),
            "appearance_constraint": item.get("appearance_constraint"),
        })
    return ids, items


def _check_speaker_classification(issues: list[dict], shot: dict, path: str,
                                   characters: dict[str, Character],
                                   classified: set[str]) -> None:
    """规划文档自带 dialogue_segments 时：说话人应归类为画内或画外，否则告警。"""
    segments = shot.get("dialogue_segments")
    if not isinstance(segments, list):
        return
    by_name = {character.name: character for character in characters.values()
               if character.name}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        speaker = segment.get("speaker")
        if not isinstance(speaker, str) or not speaker.strip():
            continue
        character = by_name.get(speaker.strip())
        if character is not None and character.id not in classified:
            _issue(issues, "warning", "speaker_unclassified", f"{path}.dialogue_segments",
                   f"说话人『{speaker}』未归类为画内或画外角色",
                   related_ids=[character.id])


def _build_shot_plan(shot: dict, character_ids: list[str],
                     on_screen_ids: list[str], offscreen_voices: list[dict],
                     visible_props: list[dict]) -> dict | None:
    """从规划文档镜头构造 shot_plan（schema v1）。

    - 六个契约字段任一提供、或出场清单非空时生成契约：完整出场清单不再被
      压成首个（character_id 仍兼容写首个）；两者均无时返回 None，
      镜头保持无契约状态，下游走旧启发式兜底；
    - 未显式声明画内角色时，默认「出场清单减去画外声音」在画内（与既有行为一致）；
    - blocking 按 character_id 提供站位，写入 on_screen_characters[].position。
    """
    has_contract = any(key in shot for key in _SHOT_PLAN_CONTRACT_KEYS)
    if not has_contract and not character_ids:
        return None
    if "on_screen_character_ids" in shot:
        screen_ids = list(on_screen_ids)
    else:
        excluded = {item["character_id"] for item in offscreen_voices}
        screen_ids = [cid for cid in character_ids if cid not in excluded]
    blocking = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
    on_screen: list[dict] = []
    for index, character_id in enumerate(screen_ids):
        entry: dict = {
            "character_id": character_id,
            "role": "primary" if index == 0 else "secondary",
        }
        position = blocking.get(character_id)
        if isinstance(position, dict) and position:
            entry["position"] = position
        elif isinstance(position, str) and position.strip():
            entry["position"] = {"note": position.strip()}
        on_screen.append(entry)
    composition = shot.get("composition")
    return {
        "version": 1,
        "source": "storyboard_plan",
        "confirmed": False,
        "visual_description": shot.get("visual_description"),
        "on_screen_characters": on_screen,
        "offscreen_voices": [
            {"character_id": item["character_id"], "kind": item["kind"]}
            for item in offscreen_voices
        ],
        "visible_props": visible_props,
        "composition": composition if isinstance(composition, dict) else None,
        "keyframe": {"t": 0, "state": shot.get("keyframe_state"), "avoid": []},
    }


def _adapt_shot(shot: dict, scene: Scene | None, character_ids: list[str],
                prop_names: list[str], evidence: list[str],
                shot_plan: dict | None) -> dict:
    return {
        "id": shot.get("shot_id"),
        "scene_id": shot.get("scene_id"),
        "episode": scene.episode if scene is not None else shot.get("episode"),
        "seq": shot.get("sequence"),
        "duration": shot.get("duration"),
        "shot_kind": shot.get("shot_kind"),
        "shot_type": shot.get("shot_type"),
        "camera_angle": shot.get("camera_angle"),
        "camera_movement": shot.get("camera_movement"),
        "dialogue": shot.get("dialogue"),
        "character_id": character_ids[0] if character_ids else None,
        "props": prop_names,
        "beats": shot.get("beat_ids") or [],
        "source_block_ids": evidence,
        # 规划层不编译 Prompt：仅透传显式提供的字段，缺失时由生产编译层生成
        "image_prompt": shot.get("image_prompt"),
        "video_prompt": None,
        # 镜头级契约（Phase 1）：导演决策落库，取代此前无人读取的 plan_meta；
        # 无契约字段的规划保持 None，下游走旧启发式兜底
        "shot_plan": shot_plan,
    }


def validate_storyboard_plan(db: Session, script_version: ScriptVersion, *,
                             project_id: str, plan: object) -> dict:
    """校验外部分镜规划并返回 Shot 兼容预览；不写库、不覆盖锁定镜头。"""
    index = build_source_index(
        script_version.content_snapshot,
        script_version_id=script_version.id,
        version=script_version.version,
    )
    structure = build_structure(index)
    block_ids = {block.source_block_id for block in index.blocks}
    scenes = {
        scene.id: scene
        for scene in db.query(Scene).filter(Scene.project_id == project_id).all()
    }
    characters = {
        row.id: row
        for row in db.query(Character).filter(Character.project_id == project_id).all()
    }
    props = {
        row.id: row
        for row in db.query(Prop).filter(Prop.project_id == project_id).all()
    }

    issues: list[dict] = []
    shots: list | None = None
    plan_version = None
    if not isinstance(plan, dict):
        _issue(issues, "error", "invalid_document", "plan", "规划文档必须是 JSON 对象")
    else:
        plan_version = plan.get("plan_version")
        if plan_version is not None and (
            not isinstance(plan_version, int) or plan_version < 1
        ):
            _issue(issues, "error", "invalid_value", "plan.plan_version",
                   "plan_version 必须是正整数")
        shots = _invalid_items(plan.get("shots"), issues)
        if shots is not None and not shots and structure["beats"]:
            _issue(issues, "error", "plan_empty", "plan.shots",
                   "剧本存在正文内容，但规划没有任何镜头")

    seen_ids: set[str] = set()
    seen_sequences: set[tuple[str, int]] = set()
    adapted: list[dict] = []
    covered_blocks: set[str] = set()
    planned_scene_keys: set[str] = set()
    ordered_scene_keys: list[str] = []

    for position, shot in enumerate(shots or []):
        path = f"plan.shots[{position}]"
        if not isinstance(shot, dict):
            _issue(issues, "error", "invalid_value", path, "镜头必须是对象")
            continue
        shot_id = shot.get("shot_id")
        if not isinstance(shot_id, str) or not shot_id.strip():
            _issue(issues, "error", "missing_field", f"{path}.shot_id",
                   "镜头缺少稳定 ID")
        elif shot_id in seen_ids:
            _issue(issues, "error", "duplicate_id", f"{path}.shot_id",
                   f"镜头 ID 重复：{shot_id}")
        else:
            seen_ids.add(shot_id)

        scene_id = shot.get("scene_id")
        scene = scenes.get(scene_id) if isinstance(scene_id, str) else None
        if not isinstance(scene_id, str) or not scene_id.strip():
            _issue(issues, "error", "missing_field", f"{path}.scene_id",
                   "镜头缺少所属场景 ID")
        elif scene is None:
            _issue(issues, "error", "scene_not_found", f"{path}.scene_id",
                   f"场景不存在或不属于当前项目：{scene_id}")
        else:
            planned_scene_keys.add(scene_key(scene.title))
            ordered_scene_keys.append(scene_key(scene.title))

        sequence = shot.get("sequence")
        if not isinstance(sequence, int) or sequence < 1:
            _issue(issues, "error", "invalid_value", f"{path}.sequence",
                   "sequence 必须是正整数")
        elif isinstance(scene_id, str) and (scene_id, sequence) in seen_sequences:
            _issue(issues, "error", "duplicate_id", f"{path}.sequence",
                   f"场景 {scene_id} 内镜头序号重复：{sequence}")
        elif isinstance(scene_id, str):
            seen_sequences.add((scene_id, sequence))

        duration = shot.get("duration")
        if not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION_SECONDS:
            _issue(issues, "error", "invalid_value", f"{path}.duration",
                   f"duration 必须是 1-{MAX_DURATION_SECONDS} 的整数秒")

        if shot.get("shot_kind") not in SHOT_KIND:
            _issue(issues, "error", "invalid_value", f"{path}.shot_kind",
                   f"shot_kind 必须是 {sorted(SHOT_KIND)} 之一")

        description = shot.get("visual_description")
        if not isinstance(description, str) or not description.strip():
            _issue(issues, "error", "missing_field", f"{path}.visual_description",
                   "镜头缺少画面描述")

        beat_ids = shot.get("beat_ids")
        if beat_ids is not None:
            valid_beats = isinstance(beat_ids, list) and all(
                isinstance(item, str) and item.strip() for item in beat_ids
            )
            if not valid_beats:
                _issue(issues, "error", "invalid_value", f"{path}.beat_ids",
                       "beat_ids 必须是非空字符串数组")

        evidence = _validate_evidence(issues, shot.get("source_block_ids"),
                                      f"{path}.source_block_ids", block_ids)
        covered_blocks.update(evidence)
        character_ids = _validate_assets(issues, shot.get("character_asset_ids"),
                                         f"{path}.character_asset_ids", characters, "角色")
        prop_ids = _validate_assets(issues, shot.get("prop_asset_ids"),
                                    f"{path}.prop_asset_ids", props, "道具")
        prop_names = [props[prop_id].name for prop_id in prop_ids]
        # --- 镜头级契约字段校验（shot_plan v1，Phase 1）---
        on_screen_ids = _validate_assets(issues, shot.get("on_screen_character_ids"),
                                         f"{path}.on_screen_character_ids",
                                         characters, "画内角色")
        if on_screen_ids and character_ids and not set(on_screen_ids) <= set(character_ids):
            _issue(issues, "error", "on_screen_not_in_cast",
                   f"{path}.on_screen_character_ids",
                   "画内角色必须属于 character_asset_ids 出场清单",
                   related_ids=sorted(set(on_screen_ids) - set(character_ids)))
        offscreen_ids, offscreen_voices = _validate_offscreen_voices(
            issues, shot.get("offscreen_voices"), f"{path}.offscreen_voices", characters)
        overlap = set(on_screen_ids) & set(offscreen_ids)
        if overlap:
            _issue(issues, "error", "on_screen_offscreen_overlap", path,
                   "同一角色不能同时出现在画内与画外", related_ids=sorted(overlap))
        visible_prop_ids, visible_props = _validate_visible_props(
            issues, shot.get("visible_props"), f"{path}.visible_props", props, characters)
        stray_props = [prop_id for prop_id in visible_prop_ids
                       if prop_id not in set(prop_ids)]
        if stray_props:
            _issue(issues, "warning", "visible_prop_not_in_cast",
                   f"{path}.visible_props",
                   "可见道具未列入 prop_asset_ids，仍按规划写入契约",
                   related_ids=stray_props)
        if len(on_screen_ids) >= 2:
            blocking = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
            missing_positions = [cid for cid in on_screen_ids if not blocking.get(cid)]
            if missing_positions:
                _issue(issues, "warning", "blocking_missing", f"{path}.blocking",
                       "画内角色 ≥2 时应为每人提供 blocking 站位",
                       related_ids=missing_positions)
        _check_speaker_classification(issues, shot, path, characters,
                                      set(on_screen_ids) | set(offscreen_ids))
        shot_plan = _build_shot_plan(shot, character_ids, on_screen_ids,
                                     offscreen_voices, visible_props)
        adapted.append(_adapt_shot(shot, scene, character_ids, prop_names,
                                   evidence, shot_plan))

    if shots:
        _check_sequences(issues, [shot for shot in shots if isinstance(shot, dict)])
    coverage = _coverage_issues(issues, structure, planned_scene_keys, covered_blocks)
    _scene_order_issues(issues, ordered_scene_keys, structure)
    existing = _existing_shots(db, project_id)
    comparison = _compare_existing(issues, [shot for shot in shots or [] if isinstance(shot, dict)],
                                   adapted, existing)

    return {
        "algorithm": PLAN_ALGORITHM,
        "script_version_id": script_version.id,
        "script_version": script_version.version,
        "plan_version": plan_version,
        "valid": not any(issue["level"] == "error" for issue in issues),
        "issues": issues,
        "coverage": coverage,
        "adapted_shots": adapted,
        "adapter": {
            "target": "shot",
            "writable": False,
            "mode": "dry_run",
            "notes": [
                "预览只做字段映射，不写入 Shot",
                "参考槽位分配与 Prompt 编译仍由生产层负责",
            ],
        },
        "comparison": comparison,
    }


def _write_fields(plan_shot: dict, adapted: dict) -> dict:
    """规划文档 → Shot 列：只取显式提供的字段，缺省项保留既有值。"""
    fields: dict = {}
    for source, column in _WRITE_FIELD_MAP:
        if source in plan_shot:
            fields[column] = plan_shot[source]
    if "character_asset_ids" in plan_shot:
        fields["character_id"] = adapted["character_id"]
    if "prop_asset_ids" in plan_shot:
        fields["props"] = adapted["props"]
    if "beat_ids" in plan_shot:
        # P1 聚合改造：不再写入 Shot.beats（与解析路径的语义冲突——解析写动作字典，规划写 ID 字符串）
        # 改为存入 aggregation_meta.source_beat_ids，保留原有 aggregation_meta 其他字段
        existing_meta = adapted.get("aggregation_meta") if isinstance(adapted.get("aggregation_meta"), dict) else {}
        fields["aggregation_meta"] = {**existing_meta, "source_beat_ids": plan_shot["beat_ids"]}
    # 空提示词不覆盖既有值：缺失/为空时交给生产编译层，避免清空已编译 Prompt
    if plan_shot.get("image_prompt"):
        fields["image_prompt"] = plan_shot["image_prompt"]
    # 镜头级契约（Phase 1）：规划生成契约时写入 shot_plan（含完整出场清单）
    if adapted.get("shot_plan") is not None:
        fields["shot_plan"] = adapted["shot_plan"]
    return fields


def apply_storyboard_plan(db: Session, script_version: ScriptVersion, *,
                          project_id: str, plan: object) -> dict:
    """把已校验的分镜规划写入既有 Shot 表（安全 upsert）。

    安全边界：
    - 规划存在 error 级问题时不写入，返回 ``applied=false``；
    - 已确认锁定镜头的 seq/duration/shot_kind 不被覆盖，列入 ``skipped_locked``；
    - 规划未包含的旧镜头不删除，列入 ``orphan_shot_ids``；
    - 规划外的字段、参考槽位、资产计划与生成任务均不改动。
    """
    preview = validate_storyboard_plan(db, script_version,
                                       project_id=project_id, plan=plan)
    base = {
        "algorithm": PLAN_APPLY_ALGORITHM,
        "mode": "upsert",
        "script_version_id": script_version.id,
        "script_version": script_version.version,
        "issues": preview["issues"],
        "coverage": preview["coverage"],
        "comparison": preview["comparison"],
    }
    empty = {
        "applied": False,
        "created_shot_ids": [],
        "updated_shot_ids": [],
        "unchanged_shot_ids": [],
        "skipped_locked": [],
        "orphan_shot_ids": [],
        "conflicts": [],
    }
    if not preview["valid"]:
        return {**base, **empty, "reason": "validation_failed"}

    plan_version = plan.get("plan_version") if isinstance(plan, dict) else None
    if not isinstance(plan_version, int) or plan_version < 1:
        return {**base, **empty, "reason": "missing_plan_version"}

    plan_shots = {
        shot["shot_id"]: shot
        for shot in plan["shots"] if isinstance(shot, dict)
    }
    adapted_by_id = {shot["id"]: shot for shot in preview["adapted_shots"]}
    involved_scene_ids = {shot["scene_id"] for shot in preview["adapted_shots"]}
    existing_by_id = {
        shot.id: shot
        for shots in _existing_shots(db, project_id).values()
        for shot in shots
    }

    # 写入前统一检查：ID 合法性/跨项目冲突、镜头换场、与保留镜头的序号冲突
    problems: list[dict] = []
    new_ids = [shot_id for shot_id in adapted_by_id if shot_id not in existing_by_id]
    for shot_id in adapted_by_id:
        if not isinstance(shot_id, str) or not shot_id.strip() or len(shot_id) > MAX_SHOT_ID_LENGTH:
            problems.append({"code": "invalid_shot_id", "shot_id": shot_id})
    if new_ids:
        foreign_ids = [
            row[0] for row in db.query(Shot.id).filter(Shot.id.in_(new_ids)).all()
        ]
        for shot_id in foreign_ids:
            problems.append({"code": "shot_id_conflict", "shot_id": shot_id})
    for shot_id, adapted in adapted_by_id.items():
        target = existing_by_id.get(shot_id)
        if target is not None and target.scene_id != adapted["scene_id"]:
            problems.append({"code": "scene_change_not_supported", "shot_id": shot_id})

    updates: list[tuple[Shot, dict]] = []
    creates: list[tuple[str, dict, dict]] = []
    skipped_locked: list[dict] = []
    unchanged: list[str] = []
    for shot_id, adapted in adapted_by_id.items():
        plan_shot = plan_shots[shot_id]
        target = existing_by_id.get(shot_id)
        if target is None:
            creates.append((shot_id, plan_shot, adapted))
            continue
        changed_locked = [
            field for field in ("seq", "duration", "shot_kind")
            if getattr(target, field) != adapted.get(field)
        ]
        if target.keyframe_confirmed and changed_locked:
            skipped_locked.append({"shot_id": shot_id, "fields": changed_locked})
            continue
        fields = _write_fields(plan_shot, adapted)
        # 锁定镜头不覆盖 shot_plan.on_screen_characters（沿用既有锁定规则）：
        # 分镜图已按当前画内角色确认，改画内清单会使确认失效
        if (target.keyframe_confirmed and fields.get("shot_plan") is not None
                and isinstance(target.shot_plan, dict)
                and target.shot_plan.get("on_screen_characters") is not None):
            merged = dict(fields["shot_plan"])
            merged["on_screen_characters"] = target.shot_plan["on_screen_characters"]
            fields["shot_plan"] = merged
        if all(getattr(target, column) == value for column, value in fields.items()):
            unchanged.append(shot_id)
            continue
        updates.append((target, fields))

    # 序号冲突：最终会占用的 (scene, episode, seq) 不能与保留镜头重叠
    updated_ids = {target.id for target, _ in updates}
    preserved = [
        shot for shot in existing_by_id.values()
        if shot.scene_id in involved_scene_ids and shot.id not in updated_ids
    ]
    preserved_keys = {
        (shot.scene_id, shot.episode, shot.seq): shot.id for shot in preserved
    }
    final_keys: dict[tuple[str, int, int], str] = {}
    for shot_id, _, adapted in creates:
        final_keys[(adapted["scene_id"], adapted["episode"], adapted["seq"])] = shot_id
    for target, fields in updates:
        final_keys[(target.scene_id, target.episode, fields.get("seq", target.seq))] = target.id
    conflicts = [
        {
            "scene_id": scene_id,
            "episode": episode,
            "seq": seq,
            "shot_id": shot_id,
            "conflicts_with": preserved_keys[(scene_id, episode, seq)],
        }
        for (scene_id, episode, seq), shot_id in final_keys.items()
        if (scene_id, episode, seq) in preserved_keys
    ]
    if problems or conflicts:
        reason = "shot_id_conflict" if any(
            item["code"] == "shot_id_conflict" for item in problems
        ) else ("sequence_conflict" if conflicts else "invalid_shot_id")
        return {**base, **empty, "reason": reason,
                "problems": problems, "conflicts": conflicts}

    orphan_shot_ids = sorted(
        shot.id for shot in existing_by_id.values()
        if shot.scene_id in involved_scene_ids and shot.id not in adapted_by_id
    )

    try:
        # 两阶段序号：先把要移动的镜头置为负序号，避开 (scene, episode, seq) 唯一约束
        for target, fields in updates:
            if fields.get("seq", target.seq) != target.seq:
                target.seq = -(abs(target.seq) or 1)
        if updates:
            db.flush()
        for target, fields in updates:
            for column, value in fields.items():
                setattr(target, column, value)
        for shot_id, plan_shot, adapted in creates:
            payload = {
                "id": shot_id,
                "scene_id": adapted["scene_id"],
                "episode": adapted["episode"],
                "seq": adapted["seq"],
                "duration": adapted["duration"],
                "shot_kind": adapted["shot_kind"],
                "status": "pending",
                "character_id": adapted["character_id"],
                "props": adapted["props"],
                "beats": adapted["beats"],
            }
            payload.update(_write_fields(plan_shot, adapted))
            db.add(Shot(**payload))
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        **base,
        "applied": True,
        "created_shot_ids": sorted(shot_id for shot_id, _, _ in creates),
        "updated_shot_ids": sorted(target.id for target, _ in updates),
        "unchanged_shot_ids": sorted(unchanged),
        "skipped_locked": sorted(skipped_locked, key=lambda item: item["shot_id"]),
        "orphan_shot_ids": orphan_shot_ids,
        "conflicts": [],
        "notes": [
            "仅写入规划显式提供的字段，缺省字段保留既有值",
            "锁定镜头的 seq/duration/shot_kind 未被覆盖",
            "规划未包含的旧镜头未删除，需人工确认后再处理",
            "未重建资产计划、未触发生成任务",
        ],
    }
