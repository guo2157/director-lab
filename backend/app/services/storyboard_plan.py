"""分镜规划契约校验、兼容 Adapter 预览与既有 Shot 对比（Phase 2 切片）。

设计约定：
- 只读：校验与转换均为 dry-run，不写 Shot、不改存量数据、不调用生成链路。
- 规划层只决定“怎么拍”，不分配参考槽位、不编译生产 Prompt；
  ``image_prompt`` 仅在规划文档显式提供时透传，缺失时留给 PromptCompiler。
- 每个镜头必须带原文区块证据，且资产引用使用真实 ID，不以名称作为唯一引用。
- 锁定/已确认镜头不覆盖：只报告冲突，Adapter 明确 ``writable=false``。
- 新结构通过 Adapter 映射为现有 Shot 字段，保持当前生产流程兼容。
"""
from itertools import pairwise

from sqlalchemy.orm import Session

from app.models import Character, Prop, Scene, ScriptVersion, Shot
from app.models.enums import SHOT_KIND
from app.services.script_analysis import build_structure, scene_key
from app.services.source_index import build_source_index

PLAN_ALGORITHM = "storyboard-plan-adapter-v1"
MAX_PLAN_SHOTS = 500
MAX_DURATION_SECONDS = 60


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
        "duration_total_seconds": sum(
            int(shot.get("duration") or 0) for shot in adapted
        ),
    }


def _adapt_shot(shot: dict, scene: Scene | None, character_ids: list[str],
                prop_names: list[str], evidence: list[str]) -> dict:
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
        "plan_meta": {
            "visual_description": shot.get("visual_description"),
            "character_asset_ids": character_ids,
            "locked": bool(shot.get("locked")),
        },
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
        adapted.append(_adapt_shot(shot, scene, character_ids, prop_names, evidence))

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
