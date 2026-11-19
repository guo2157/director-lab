#!/usr/bin/env python3
"""工作流种子导入：将标准工作流定义及其能力声明写入 workflow 表。

读取 workflow_dir 下的 wf_*.json，用 parse_workflow 校验，
通过 normalize_capabilities 自动解析能力（不再依赖 .meta.json）。
按 name 幂等更新或插入。

版本规则：仅当内容（workflow_type/description/trigger/capabilities/definition/parsed_schema）
实际变化时才 version+1，内容一致的重复播种不改动记录、不虚增版本。

描述保护：description_source='user' 的记录不覆盖其描述，保留用户修改。
运行方式：在 backend 目录执行 .venv/bin/python scripts/seed_workflows.py
"""
import hashlib
import json
import sys
from pathlib import Path

# 允许以脚本方式直接运行时定位到 backend 包根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Workflow
from app.services.workflow_inject import parse_workflow
from app.services.workflow import versions as versions_svc
from app.services.workflow.schema_builder import build_schema
from app.services.workflow.capabilities import normalize as normalize_capabilities

settings = get_settings()

# 内置工作流用户友好描述映射（不依赖 .meta.json 里的技术说明）
_BUILTIN_DESCRIPTIONS: dict[str, str] = {
    "wf_image_t2i_qwen21_turbo": "千问文生图",
    "wf_image_ref_qwen21_multi": "千问多参考图生图",
    "wf_video_h3_t2v": "文生视频",
    "wf_video_h3_flf2v": "首尾帧视频",
    "wf_video_h3_ref2va_std_5s": "标准5秒图生视频",
    "wf_video_h3_ref2va_fast_5s": "极速5秒图生视频",
    "wf_video_h3_ref2va_quality_10s": "高清10秒图生视频",
    "wf_tts_qwen3_voice_clone": "声音克隆",
    "wf_tts_qwen3_custom_voice": "自定义语音",
    "wf_bgm_stable_audio": "背景音乐生成",
}


def load_json(p: Path):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def canonical(obj):
    """JSON 字段的内容指纹：键排序后序列化，忽略键序差异。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _detect_trigger(workflow_type: str, definition: dict) -> dict | None:
    """根据 workflow_type 和定义自动推导 trigger（不再依赖 .meta.json）。"""
    if workflow_type == "image":
        return {"target": "keyframe", "required_params": [], "priority": 10}
    if workflow_type == "video":
        return {"target": "clip", "required_params": [], "priority": 10}
    if workflow_type in ("tts", "bgm", "sfx"):
        return {
            "target": workflow_type,
            "required_params": [],
            "priority": 10,
        }
    return None


def seed():
    wf_dir = Path(settings.workflow_dir)
    workflows = sorted(
        f for f in wf_dir.glob("wf_*.json") if not f.name.endswith(".meta.json")
    )
    if not workflows:
        print(f"[severe] 在 {wf_dir} 下找不到 wf_*.json")
        sys.exit(1)
    print(f"[event] 发现 {len(workflows)} 份工作流")
    db = SessionLocal()
    created = updated = unchanged = skipped = 0
    try:
        for wf_path in workflows:
            name = wf_path.stem
            definition = load_json(wf_path)

            # 校验解析：标记解析 + 画布残留键检测；错误也落库以便排查，不阻断导入
            parsed = parse_workflow(definition)

            # 从文件名前缀推导 workflow_type（不再读 .meta.json）
            workflow_type = "unknown"
            # 1. 文件名前缀最可靠（seed 目录约定 wf_{type}_*）
            _fn = name.lower()
            if _fn.startswith("wf_video_"):
                workflow_type = "video"
            elif _fn.startswith("wf_image_"):
                workflow_type = "image"
            elif _fn.startswith("wf_tts_"):
                workflow_type = "tts"
            elif _fn.startswith("wf_bgm_"):
                workflow_type = "bgm"
            elif _fn.startswith("wf_sfx_"):
                workflow_type = "sfx"

            # 2. 文件名无前缀时，从 class_type 推断（独特节点优先，避免 audio/vae/clip 误判）
            if workflow_type == "unknown":
                class_types = set()
                for node in definition.values():
                    if isinstance(node, dict):
                        ct = node.get("class_type", "")
                        if ct:
                            class_types.add(ct.lower())
                if any(
                    "minimaxh3" in ct or "createvideo" in ct or "savevideo" in ct
                    for ct in class_types
                ):
                    workflow_type = "video"
                elif any(
                    "textencodeqwenimage21" in ct
                    or "saveimage" in ct
                    or "emptylatentimage" in ct
                    for ct in class_types
                ):
                    workflow_type = "image"
                elif any(
                    "qwen3customvoice" in ct or "qwen3voiceclone" in ct
                    for ct in class_types
                ):
                    workflow_type = "tts"
                elif any(
                    "stableaudio" in ct or "conditioningstableaudio" in ct
                    for ct in class_types
                ):
                    workflow_type = "bgm"
                elif any("sfx" in ct for ct in class_types):
                    workflow_type = "sfx"

            # 3. 兜底：用 normalize_capabilities 二次确认
            if workflow_type == "unknown":
                try:
                    raw_caps = normalize_capabilities(
                        "unknown", definition, capabilities=None, trigger=None
                    )
                    resolved_type = (
                        raw_caps.get("workflow_type")
                        if isinstance(raw_caps, dict)
                        else None
                    )
                    if resolved_type and resolved_type != "unknown":
                        workflow_type = resolved_type
                except Exception:
                    pass

            # 自动生成能力声明（不再读 .meta.json）
            trigger = _detect_trigger(workflow_type, definition)
            capabilities = normalize_capabilities(
                workflow_type, definition, capabilities=None, trigger=trigger
            )

            # 使用内置描述映射，兜底留空
            description = _BUILTIN_DESCRIPTIONS.get(name)

            # 与上传端点共用同一 Schema 构建器
            parsed_schema = build_schema(
                definition, capabilities=capabilities, trigger=trigger
            )

            status_msg = "OK" if not parsed.errors else f"ERROR({len(parsed.errors)})"
            print(
                f"  [{status_msg}] {name}  type={workflow_type}  "
                f"params={len(parsed.params)} outputs={len(parsed.outputs)}"
            )

            existing = db.query(Workflow).filter_by(name=name).first()
            file_hash = hashlib.sha256(
                canonical(definition).encode("utf-8")
            ).hexdigest()

            if existing:
                # 用户已手动修改描述时，跳过描述覆盖
                if existing.description_source == "user":
                    description = existing.description

                changed = (
                    existing.workflow_type != workflow_type
                    or existing.description != description
                    or canonical(existing.trigger) != canonical(trigger)
                    or canonical(existing.capabilities) != canonical(capabilities)
                    or canonical(existing.definition) != canonical(definition)
                    or canonical(existing.parsed_schema) != canonical(parsed_schema)
                    or existing.original_filename != wf_path.name
                    or existing.file_hash != file_hash
                    or not existing.is_builtin
                )

                if not changed:
                    unchanged += 1
                    continue

                existing.workflow_type = workflow_type
                if existing.description_source != "user":
                    existing.description = description
                existing.trigger = trigger
                existing.capabilities = capabilities
                existing.definition = definition
                existing.parsed_schema = parsed_schema
                existing.original_filename = wf_path.name
                existing.file_hash = file_hash
                existing.is_builtin = True
                # 标记描述来源为 seed（除非用户已改）
                if existing.description_source != "user":
                    existing.description_source = "seed"
                # 定义变化生成不可变版本并发布
                versions_svc.create_version(
                    db, existing, definition, changelog="seed 更新", publish=True
                )
                updated += 1
            else:
                wf_row = Workflow(
                    name=name,
                    original_filename=wf_path.name,
                    file_hash=file_hash,
                    workflow_type=workflow_type,
                    description=description,
                    description_source="seed",
                    trigger=trigger,
                    capabilities=capabilities,
                    definition=definition,
                    parsed_schema=parsed_schema,
                    version=0,
                    is_default=False,
                    is_builtin=True,
                )
                db.add(wf_row)
                db.flush()
                versions_svc.create_version(
                    db, wf_row, definition, changelog="seed 导入", publish=True
                )
                created += 1

        db.commit()
        print(
            f"[done] 新增 {created}，更新 {updated}，"
            f"未变化 {unchanged}，跳过 {skipped}，"
            f"共 {created + updated + unchanged + skipped}"
        )
    except Exception as e:
        db.rollback()
        print(f"[failed] {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
