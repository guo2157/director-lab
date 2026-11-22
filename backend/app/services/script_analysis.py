"""剧本结构分析骨架、外部分析文档校验与 Shadow Run 对比（Phase 1 切片 3）。

设计约定：
- 只读：不写数据库、不改剧本、不触发解析或生成。
- 结构分析完全由原文确定性派生（行级 SourceIndex），不调用语言模型。
  故事梗概、主题、人物关系等语义字段必须由外部分析文档提供，并走
  ``validate_analysis`` 校验；本模块不凭空生成这些内容。
- 所有结论带原文区块证据；推断字段必须标记 ``origin=inference``，
  不得把创意推断冒充成剧本事实。
- Shadow Run 与现有 Scene/Shot/Character/Prop 并行比较，只报告差异，
  不覆盖生产数据；结果不落库，持久化留待后续迁移切片。
"""
import hashlib
import re

from sqlalchemy.orm import Session

from app.models import Character, Prop, Scene, ScriptVersion, Shot
from app.services.source_index import SourceIndex, build_source_index

ANALYSIS_ALGORITHM = "source-structure-v1"
SHADOW_RUN_ALGORITHM = "shadow-run-v1"

MAX_ANALYSIS_ITEMS = 5000
DIALOGUE_MAX_CHARS = 40
_RISK_LEVELS = frozenset({"low", "medium", "high"})

_NUMBER = r"(?:\d+|[零〇一二三四五六七八九十百千万两]+)"
_SCENE_PREFIX_RE = re.compile(
    rf"^\s*(?:第\s*{_NUMBER}\s*场|场次\s*\d+|\d+\s*[.、\-)]|"
    r"INT\.?\s*/\s*EXT\.?|INT\.?|EXT\.?|I/E\.?)\s*[:：]?\s*",
    re.IGNORECASE,
)
_REF_RE = re.compile(r"@([\w\u4e00-\u9fff·]+)")
_SPEAKER_RE = re.compile(r"^\s*([^\s：:]{1,40})\s*[：:]\s*\S")
_TIME_VALUES = (
    "清晨", "凌晨", "早晨", "早上", "上午", "中午", "午后", "下午",
    "傍晚", "黄昏", "夜晚", "深夜", "夜里", "白天", "晚上", "日", "夜", "晨", "昏",
)
_INTERIOR_VALUES = ("内外景", "外内景", "内外", "外内", "内景", "外景", "内", "外")
_HEADER_SEPARATORS = re.compile(r"[\s/／|]+")


def _analysis_id(prefix: str, index: SourceIndex, block_id: str) -> str:
    """分析对象 ID 由算法、剧本版本与原文区块派生，保证同输入同结果。"""
    evidence = "\0".join((
        ANALYSIS_ALGORITHM, index.script_version_id,
        str(index.script_version), block_id,
    ))
    return f"{prefix}_{hashlib.sha256(evidence.encode('utf-8')).hexdigest()[:12]}"


def parse_scene_heading(text: str) -> dict:
    """从场景标题行提取地点/时段/内外景；无法判断的字段保持 None。

    只做标题文本的确定性切分，不做语义推断：切片结果属于原文派生信息。
    """
    body = _SCENE_PREFIX_RE.sub("", text or "", count=1).strip()
    tokens = [
        token for token in _HEADER_SEPARATORS.split(body)
        if token and token not in {"-", "—", "–", "－"}
    ]
    time_of_day = next((token for token in tokens if token in _TIME_VALUES), None)
    interior_exterior = next((token for token in tokens if token in _INTERIOR_VALUES), None)
    location_tokens = [
        token for token in tokens
        if token not in _TIME_VALUES and token not in _INTERIOR_VALUES
    ]
    return {
        "location": " ".join(location_tokens) or None,
        "time_of_day": time_of_day,
        "interior_exterior": interior_exterior,
    }


def scene_key(title: str) -> str:
    """场景标题归一化：去掉场次前缀、时段与内外景标记，用于对比比较。"""
    body = _SCENE_PREFIX_RE.sub("", title or "", count=1).strip()
    tokens = [
        token for token in _HEADER_SEPARATORS.split(body)
        if token and token not in {"-", "—", "–", "－"}
    ]
    kept = [
        token for token in tokens
        if token not in _TIME_VALUES and token not in _INTERIOR_VALUES
    ]
    return "".join(kept).strip().casefold()


def _mentioned_names(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in _REF_RE.finditer(text or "")))


def build_structure(index: SourceIndex) -> dict:
    """从原文索引确定性派生集/场景/Beat 骨架与实体提及，不访问数据库。"""
    episodes: list[dict] = []
    scenes: list[dict] = []
    beats: list[dict] = []
    orphan_block_ids: list[str] = []
    current_episode: dict | None = None
    current_scene: dict | None = None

    for block in index.blocks:
        if block.kind == "episode":
            current_episode = {
                "episode_ref": _analysis_id("aep", index, block.source_block_id),
                "title": block.text.strip(),
                "source_block_ids": [block.source_block_id],
                "scene_ids": [],
            }
            episodes.append(current_episode)
            current_scene = None
            continue
        if block.kind == "scene":
            current_scene = {
                "scene_id": _analysis_id("asc", index, block.source_block_id),
                "episode_ref": current_episode["episode_ref"] if current_episode else None,
                "title": block.text.strip(),
                "source_block_ids": [block.source_block_id],
                "body_source_block_ids": [],
                **parse_scene_heading(block.text),
            }
            scenes.append(current_scene)
            if current_episode is not None:
                current_episode["scene_ids"].append(current_scene["scene_id"])
            continue
        beat = {
            "beat_id": _analysis_id("abt", index, block.source_block_id),
            "scene_id": current_scene["scene_id"] if current_scene else None,
            "source_block_ids": [block.source_block_id],
            "kind": block.kind,
            "text": block.text,
            "mentioned_names": _mentioned_names(block.text),
        }
        if block.kind == "dialogue":
            speaker = _SPEAKER_RE.match(block.text)
            if speaker is not None:
                name = speaker.group(1).strip()
                if name and name not in beat["mentioned_names"]:
                    beat["mentioned_names"].append(name)
        beats.append(beat)
        if current_scene is not None:
            current_scene["body_source_block_ids"].append(block.source_block_id)
        else:
            orphan_block_ids.append(block.source_block_id)

    return {
        "algorithm": ANALYSIS_ALGORITHM,
        "script_version_id": index.script_version_id,
        "script_version": index.script_version,
        "episodes": episodes,
        "scenes": scenes,
        "beats": beats,
        "mentioned_names": list(dict.fromkeys(
            name for beat in beats for name in beat["mentioned_names"]
        )),
        "coverage": {
            "block_count": len(index.blocks),
            "scene_heading_count": len(scenes),
            "beat_count": len(beats),
            "blocks_outside_scene": orphan_block_ids,
        },
    }


def _project_entity_index(db: Session, project_id: str) -> dict[str, dict[str, list[str]]]:
    """项目内同名实体可能跨类型存在，因此按名称聚合 ID 列表而非取第一条。"""
    index: dict[str, dict[str, list[str]]] = {
        "character": {}, "scene": {}, "prop": {},
    }
    for row in db.query(Character).filter(Character.project_id == project_id).all():
        index["character"].setdefault(row.name, []).append(row.id)
    for row in db.query(Scene).filter(Scene.project_id == project_id).all():
        index["scene"].setdefault(row.title, []).append(row.id)
    for row in db.query(Prop).filter(Prop.project_id == project_id).all():
        index["prop"].setdefault(row.name, []).append(row.id)
    return index


def _classify_mentions(db: Session, project_id: str, structure: dict) -> dict:
    project_index = _project_entity_index(db, project_id)
    block_by_name: dict[str, list[str]] = {}
    for beat in structure["beats"]:
        for name in beat["mentioned_names"]:
            ids = block_by_name.setdefault(name, [])
            block_id = beat["source_block_ids"][0]
            if block_id not in ids:
                ids.append(block_id)
    entities: dict[str, list[dict]] = {"characters": [], "scenes": [], "props": []}
    unlinked: list[dict] = []
    ambiguous: list[dict] = []
    labels = (
        ("character", "characters"),
        ("scene", "scenes"),
        ("prop", "props"),
    )
    for name in structure["mentioned_names"]:
        block_ids = block_by_name.get(name, [])
        matched = [
            label for entity_type, label in labels
            if project_index[entity_type].get(name)
        ]
        if len(matched) > 1:
            ambiguous.append({
                "name": name,
                "entity_types": [entity_type for entity_type, _ in labels
                                 if project_index[entity_type].get(name)],
                "source_block_ids": block_ids,
            })
            continue
        if not matched:
            unlinked.append({"name": name, "source_block_ids": block_ids})
            continue
        entity_type, label = next(
            (entity_type, label) for entity_type, label in labels
            if project_index[entity_type].get(name)
        )
        entities[label].append({
            "name": name,
            "entity_ids": project_index[entity_type][name],
            "source_block_ids": block_ids,
        })
    return {"entities": entities, "unlinked": unlinked, "ambiguous": ambiguous}


def _build_risks(structure: dict, classification: dict) -> list[dict]:
    risks: list[dict] = []
    for item in classification["unlinked"]:
        risks.append({
            "code": "reference_unlinked",
            "level": "medium",
            "message": f"原文引用的『{item['name']}』未匹配到项目角色、场景或道具",
            "source_block_ids": item["source_block_ids"],
            "related_ids": [item["name"]],
            "suggestion": "补齐实体资产或修正 @引用名称后再进入生产",
        })
    for item in classification["ambiguous"]:
        risks.append({
            "code": "reference_ambiguous",
            "level": "medium",
            "message": f"『{item['name']}』同时匹配多种实体类型，无法确定引用对象",
            "source_block_ids": item["source_block_ids"],
            "related_ids": [item["name"]],
            "suggestion": "改名消除同名后重新分析",
        })
    if structure["beats"] and not structure["scenes"]:
        risks.append({
            "code": "scene_structure_missing",
            "level": "high",
            "message": "剧本有正文但没有可识别的场景标题，无法建立 Beat 归属",
            "source_block_ids": [beat["source_block_ids"][0]
                                 for beat in structure["beats"][:5]],
            "related_ids": [],
            "suggestion": "补充『第N场 地点 时间 内外景』标题后重新分析",
        })
    orphan_ids = structure["coverage"]["blocks_outside_scene"]
    if orphan_ids:
        risks.append({
            "code": "block_outside_scene",
            "level": "low",
            "message": f"{len(orphan_ids)} 个正文区块位于任何场景标题之前",
            "source_block_ids": orphan_ids,
            "related_ids": [],
            "suggestion": "确认这些内容是否属于开场字幕或需要补场景标题",
        })

    character_names = {item["name"] for item in classification["entities"]["characters"]}
    scene_names: dict[str, list[str]] = {}
    for beat in structure["beats"]:
        scene_id = beat["scene_id"]
        if scene_id is None:
            continue
        names = scene_names.setdefault(scene_id, [])
        for name in beat["mentioned_names"]:
            if name in character_names and name not in names:
                names.append(name)
    for scene in structure["scenes"]:
        names = scene_names.get(scene["scene_id"], [])
        if len(names) >= 3:
            risks.append({
                "code": "multi_character_scene",
                "level": "medium",
                "message": f"场景『{scene['title']}』涉及 {len(names)} 个角色，"
                           "存在身份混淆与参考槽位不足风险",
                "source_block_ids": scene["body_source_block_ids"],
                "related_ids": [scene["scene_id"], *names],
                "suggestion": "拆分场景或改用支持更多角色参考槽位的工作流",
            })
    for beat in structure["beats"]:
        if beat["kind"] == "dialogue" and len(beat["text"]) >= DIALOGUE_MAX_CHARS:
            risks.append({
                "code": "dialogue_too_long",
                "level": "low",
                "message": f"对白区块长度 {len(beat['text'])} 字符，可能挤压镜头时长",
                "source_block_ids": beat["source_block_ids"],
                "related_ids": [beat["beat_id"]],
                "suggestion": "拆分为多句对白或调整镜头时长",
            })
    return risks


def analyze_script_version(db: Session, script_version: ScriptVersion, *,
                           project_id: str) -> dict:
    """构建剧本结构骨架并附着项目实体关联与结构化风险；不写库。"""
    index = build_source_index(
        script_version.content_snapshot,
        script_version_id=script_version.id,
        version=script_version.version,
    )
    structure = build_structure(index)
    classification = _classify_mentions(db, project_id, structure)
    return {
        **structure,
        "entities": classification["entities"],
        "unlinked_references": classification["unlinked"],
        "ambiguous_references": classification["ambiguous"],
        "risks": _build_risks(structure, classification),
    }


def _evidence_issue(issues: list[dict], value: object, path: str,
                    block_ids: set[str]) -> None:
    if not isinstance(value, list) or not value:
        issues.append({
            "code": "missing_field", "path": path,
            "message": "缺少原文证据 source_block_ids",
        })
        return
    for position, block_id in enumerate(value):
        if not isinstance(block_id, str) or not block_id:
            issues.append({
                "code": "invalid_value", "path": f"{path}[{position}]",
                "message": "source_block_id 必须是非空字符串",
            })
        elif block_id not in block_ids:
            issues.append({
                "code": "source_block_not_found", "path": f"{path}[{position}]",
                "message": "原文区块不存在或不属于当前剧本版本",
                "source_block_id": block_id,
            })


def _check_story_origins(issues: list[dict], value: object, path: str,
                         block_ids: set[str]) -> None:
    """递归检查故事分析中的 origin 标记：source 必须有可解析的原文证据。"""
    if isinstance(value, dict):
        origin = value.get("origin")
        if origin is not None:
            if origin not in ("source", "inference"):
                issues.append({
                    "code": "invalid_value", "path": f"{path}.origin",
                    "message": "origin 只能是 source 或 inference",
                })
            elif origin == "source":
                evidence = value.get("source_block_ids")
                if not isinstance(evidence, list) or not evidence:
                    issues.append({
                        "code": "source_claim_without_evidence",
                        "path": f"{path}.source_block_ids",
                        "message": "标记为 source 的结论必须附原文证据",
                    })
                else:
                    for position, block_id in enumerate(evidence):
                        if block_id not in block_ids:
                            issues.append({
                                "code": "source_block_not_found",
                                "path": f"{path}.source_block_ids[{position}]",
                                "message": "原文区块不存在或不属于当前剧本版本",
                                "source_block_id": block_id,
                            })
        for key, item in value.items():
            _check_story_origins(issues, item, f"{path}.{key}", block_ids)
    elif isinstance(value, list):
        for position, item in enumerate(value):
            _check_story_origins(issues, item, f"{path}[{position}]", block_ids)


def validate_analysis(document: object, index: SourceIndex) -> dict:
    """校验外部分析文档（故事/场景/Beat/资产/风险）与原文证据的一致性。"""
    issues: list[dict] = []
    block_ids = {block.source_block_id for block in index.blocks}
    checked = {"scenes": 0, "beats": 0, "assets": 0, "risks": 0}

    if not isinstance(document, dict):
        return {
            "valid": False, "checked": checked,
            "issues": [{"code": "invalid_document", "path": "analysis",
                        "message": "分析文档必须是 JSON 对象"}],
        }
    for key in ("story", "scenes", "beats", "assets", "risks"):
        if key not in document:
            issues.append({
                "code": "missing_field", "path": key,
                "message": "缺少必需的分析分区",
            })

    story = document.get("story")
    if story is not None and not isinstance(story, dict):
        issues.append({
            "code": "invalid_value", "path": "story",
            "message": "story 必须是对象",
        })
    elif isinstance(story, dict):
        _check_story_origins(issues, story, "story", block_ids)

    scenes = document.get("scenes")
    if scenes is not None and not isinstance(scenes, list):
        issues.append({
            "code": "invalid_value", "path": "scenes", "message": "scenes 必须是数组",
        })
        scenes = None
    elif isinstance(scenes, list) and len(scenes) > MAX_ANALYSIS_ITEMS:
        issues.append({
            "code": "too_many_items", "path": "scenes",
            "message": f"场景数量超过上限 {MAX_ANALYSIS_ITEMS}",
        })
        scenes = None

    scene_ids: set[str] = set()
    for position, scene in enumerate(scenes or []):
        checked["scenes"] += 1
        path = f"scenes[{position}]"
        if not isinstance(scene, dict):
            issues.append({
                "code": "invalid_value", "path": path, "message": "场景必须是对象",
            })
            continue
        scene_id = scene.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id.strip():
            issues.append({
                "code": "missing_field", "path": f"{path}.scene_id",
                "message": "场景缺少稳定 ID",
            })
        elif scene_id in scene_ids:
            issues.append({
                "code": "duplicate_id", "path": f"{path}.scene_id",
                "message": f"场景 ID 重复：{scene_id}",
            })
        else:
            scene_ids.add(scene_id)
        title = scene.get("title")
        if not isinstance(title, str) or not title.strip():
            issues.append({
                "code": "missing_field", "path": f"{path}.title",
                "message": "场景缺少名称",
            })
        _evidence_issue(issues, scene.get("source_block_ids"),
                        f"{path}.source_block_ids", block_ids)

    beats = document.get("beats")
    if beats is not None and not isinstance(beats, list):
        issues.append({
            "code": "invalid_value", "path": "beats", "message": "beats 必须是数组",
        })
        beats = None
    elif isinstance(beats, list) and len(beats) > MAX_ANALYSIS_ITEMS:
        issues.append({
            "code": "too_many_items", "path": "beats",
            "message": f"Beat 数量超过上限 {MAX_ANALYSIS_ITEMS}",
        })
        beats = None

    beat_ids: set[str] = set()
    for position, beat in enumerate(beats or []):
        checked["beats"] += 1
        path = f"beats[{position}]"
        if not isinstance(beat, dict):
            issues.append({
                "code": "invalid_value", "path": path, "message": "Beat 必须是对象",
            })
            continue
        beat_id = beat.get("beat_id")
        if not isinstance(beat_id, str) or not beat_id.strip():
            issues.append({
                "code": "missing_field", "path": f"{path}.beat_id",
                "message": "Beat 缺少稳定 ID",
            })
        elif beat_id in beat_ids:
            issues.append({
                "code": "duplicate_id", "path": f"{path}.beat_id",
                "message": f"Beat ID 重复：{beat_id}",
            })
        else:
            beat_ids.add(beat_id)
        scene_id = beat.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id.strip():
            issues.append({
                "code": "missing_field", "path": f"{path}.scene_id",
                "message": "Beat 缺少所属场景 ID",
            })
        elif scene_id not in scene_ids:
            issues.append({
                "code": "beat_scene_not_found", "path": f"{path}.scene_id",
                "message": f"Beat 引用的场景不存在：{scene_id}",
            })
        event = beat.get("event")
        if not isinstance(event, str) or not event.strip():
            issues.append({
                "code": "missing_field", "path": f"{path}.event",
                "message": "Beat 缺少事件描述",
            })
        _evidence_issue(issues, beat.get("source_block_ids"),
                        f"{path}.source_block_ids", block_ids)

    assets = document.get("assets")
    if assets is not None and not isinstance(assets, dict):
        issues.append({
            "code": "invalid_value", "path": "assets", "message": "assets 必须是对象",
        })
        assets = None
    for category in ("characters", "locations", "props"):
        items = (assets or {}).get(category)
        if items is None:
            continue
        if not isinstance(items, list):
            issues.append({
                "code": "invalid_value", "path": f"assets.{category}",
                "message": f"{category} 必须是数组",
            })
            continue
        names: set[str] = set()
        for position, item in enumerate(items):
            checked["assets"] += 1
            path = f"assets.{category}[{position}]"
            if not isinstance(item, dict):
                issues.append({
                    "code": "invalid_value", "path": path,
                    "message": "资产条目必须是对象",
                })
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                issues.append({
                    "code": "missing_field", "path": f"{path}.name",
                    "message": "资产条目缺少名称",
                })
            elif name in names:
                issues.append({
                    "code": "duplicate_id", "path": f"{path}.name",
                    "message": f"资产名称重复：{name}",
                })
            else:
                names.add(name)
            _evidence_issue(issues, item.get("source_block_ids"),
                            f"{path}.source_block_ids", block_ids)

    risks = document.get("risks")
    if risks is not None and not isinstance(risks, list):
        issues.append({
            "code": "invalid_value", "path": "risks", "message": "risks 必须是数组",
        })
        risks = None
    for position, risk in enumerate(risks or []):
        checked["risks"] += 1
        path = f"risks[{position}]"
        if not isinstance(risk, dict):
            issues.append({
                "code": "invalid_value", "path": path, "message": "风险条目必须是对象",
            })
            continue
        for field in ("code", "message", "suggestion"):
            value = risk.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append({
                    "code": "missing_field", "path": f"{path}.{field}",
                    "message": f"风险条目缺少 {field}",
                })
        if risk.get("level") not in _RISK_LEVELS:
            issues.append({
                "code": "invalid_value", "path": f"{path}.level",
                "message": "风险级别只能是 low/medium/high",
            })
        _evidence_issue(issues, risk.get("source_block_ids"),
                        f"{path}.source_block_ids", block_ids)

    return {"valid": not issues, "checked": checked, "issues": issues}


def _production_baseline(db: Session, project_id: str) -> dict:
    scenes = db.query(Scene).filter(Scene.project_id == project_id).all()
    characters = db.query(Character).filter(Character.project_id == project_id).all()
    props = db.query(Prop).filter(Prop.project_id == project_id).all()
    shot_count = (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .filter(Scene.project_id == project_id)
        .count()
    )
    return {
        "scene_count": len(scenes),
        "shot_count": shot_count,
        "character_count": len(characters),
        "prop_count": len(props),
        "scene_titles": [scene.title for scene in scenes],
        "character_names": [character.name for character in characters],
        "prop_names": [prop.name for prop in props],
    }


def _diff_sets(analysis: set[str], production: set[str]) -> dict:
    return {
        "matched": sorted(analysis & production),
        "only_in_analysis": sorted(analysis - production),
        "only_in_production": sorted(production - analysis),
    }


def _compare_with_production(structure: dict, entities: dict, unlinked: list[dict],
                             baseline: dict) -> dict:
    analysis_scene_keys = {
        scene_key(scene.get("location") or scene["title"]) for scene in structure["scenes"]
    }
    production_scene_keys = {scene_key(title) for title in baseline["scene_titles"]}
    analysis_characters = {item["name"] for item in entities["characters"]}
    analysis_props = {item["name"] for item in entities["props"]}
    return {
        "scenes": _diff_sets(analysis_scene_keys, production_scene_keys),
        "characters": _diff_sets(analysis_characters, set(baseline["character_names"])),
        "props": _diff_sets(analysis_props, set(baseline["prop_names"])),
        "unlinked_references": sorted(item["name"] for item in unlinked),
        "counts": {
            "analysis_scenes": len(structure["scenes"]),
            "production_scenes": baseline["scene_count"],
            "analysis_beats": len(structure["beats"]),
            "production_shots": baseline["shot_count"],
        },
    }


def shadow_run(db: Session, script_version: ScriptVersion, *, project_id: str,
               analysis: dict | None = None) -> dict:
    """只读 Shadow Run：结构分析与现有生产数据并行比较，不写库、不覆盖。"""
    index = build_source_index(
        script_version.content_snapshot,
        script_version_id=script_version.id,
        version=script_version.version,
    )
    structure = build_structure(index)
    classification = _classify_mentions(db, project_id, structure)
    baseline = _production_baseline(db, project_id)
    document = analyze_script_version(db, script_version, project_id=project_id)
    return {
        "algorithm": SHADOW_RUN_ALGORITHM,
        "script_version_id": script_version.id,
        "script_version": script_version.version,
        "structure": document,
        "production_baseline": baseline,
        "comparison": _compare_with_production(
            structure, classification["entities"], classification["unlinked"], baseline,
        ),
        "validation": validate_analysis(analysis, index) if analysis is not None else None,
        "notes": [
            "只读对比：不写入数据库，不覆盖现有解析结果与生产数据",
            "结构分析不调用语言模型；语义字段需外部提供并单独校验",
        ],
    }
