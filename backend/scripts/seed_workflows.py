#!/usr/bin/env python3
"""工作流种子导入：将标准工作流定义及其能力声明写入 workflow 表。

读取 workflow_dir 下的 wf_*.json 与配套 wf_*.meta.json，逐份用 parse_workflow 校验
（同一套标记规则，含画布残留键检测），再按 name 幂等更新或插入。
版本规则：仅当内容（workflow_type/description/trigger/capabilities/definition/parsed_schema）
实际变化时才 version+1，内容一致的重复播种不改动记录、不虚增版本。
运行方式：在 backend 目录执行 .venv/bin/python scripts/seed_workflows.py
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

# 允许以脚本方式直接运行时定位到 backend 包根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Workflow
from app.services.workflow_inject import parse_workflow

settings = get_settings()


def load_json(p: Path):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def canonical(obj):
    """JSON 字段的内容指纹：键排序后序列化，忽略键序差异。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    created = updated = unchanged = 0
    try:
        for wf_path in workflows:
            name = wf_path.stem
            meta_path = wf_path.with_suffix(".meta.json")
            definition = load_json(wf_path)
            meta = load_json(meta_path) if meta_path.exists() else {}
            trigger = meta.get("trigger")
            capabilities = meta.get("capabilities")
            workflow_type = (capabilities or {}).get("workflow_type") or "unknown"
            description = (capabilities or {}).get("notes")
            # 校验解析：标记解析 + 画布残留键检测；错误也落库以便排查，不阻断导入
            parsed = parse_workflow(definition)
            parsed_schema = {
                "params": {k: asdict(v) for k, v in parsed.params.items()},
                "outputs": sorted(parsed.outputs),
                "errors": list(parsed.errors),
                "warnings": list(parsed.warnings),
            }
            status = "OK" if not parsed.errors else f"ERROR({len(parsed.errors)})"
            print(
                f"  [{status}] {name}  type={workflow_type}  "
                f"params={len(parsed.params)} outputs={len(parsed.outputs)}"
            )
            existing = db.query(Workflow).filter_by(name=name).first()
            if existing:
                changed = (
                    existing.workflow_type != workflow_type
                    or existing.description != description
                    or canonical(existing.trigger) != canonical(trigger)
                    or canonical(existing.capabilities) != canonical(capabilities)
                    or canonical(existing.definition) != canonical(definition)
                    or canonical(existing.parsed_schema) != canonical(parsed_schema)
                    or not existing.is_builtin
                )
                if not changed:
                    unchanged += 1
                    continue
                existing.workflow_type = workflow_type
                existing.description = description
                existing.trigger = trigger
                existing.capabilities = capabilities
                existing.definition = definition
                existing.parsed_schema = parsed_schema
                existing.is_builtin = True
                existing.version = (existing.version or 0) + 1
                updated += 1
            else:
                db.add(Workflow(
                    name=name,
                    workflow_type=workflow_type,
                    description=description,
                    trigger=trigger,
                    capabilities=capabilities,
                    definition=definition,
                    parsed_schema=parsed_schema,
                    version=1,
                    is_default=False,
                    is_builtin=True,
                ))
                created += 1
        db.commit()
        print(f"[done] 新增 {created}，更新 {updated}，未变化 {unchanged}，共 {created + updated + unchanged}")
    except Exception as e:
        db.rollback()
        print(f"[failed] {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
