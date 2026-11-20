"""一次性回填：为历史工作流写入 model_type（模型类型/细分大类）。

取值完全来自 capabilities.normalize() 的推导结果，不按名称/描述/文件名猜测。
判不出来的保持 NULL，前端会显示「待检查」，不会硬塞进某类。

用法：.venv/bin/python scripts/backfill_model_type.py [--dry-run]
"""
import sys

sys.path.insert(0, ".")

from app.core.database import SessionLocal  # noqa: E402
from app.models import Workflow  # noqa: E402
from app.api.v1.workflows import (  # noqa: E402
    _classify_by_capabilities,
    _slot_selection_caps,
)


def main(dry_run: bool = False) -> int:
    db = SessionLocal()
    try:
        rows = db.query(Workflow).all()
        changed = 0
        for wf in rows:
            caps = _slot_selection_caps(wf)
            derived = _classify_by_capabilities(wf.workflow_type, caps)
            if wf.model_type == derived:
                continue
            print(f"{wf.name:42s} {wf.workflow_type:6s} {wf.model_type!r} -> {derived!r}")
            if not dry_run:
                wf.model_type = derived
            changed += 1
        if not dry_run:
            db.commit()
        print(f"total={len(rows)} changed={changed} dry_run={dry_run}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main("--dry-run" in sys.argv))
