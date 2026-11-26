#!/usr/bin/env python
"""补齐缺失的资产缩略图（thumb_path 指向不存在的文件时重新生成）。

背景
----
分镜产物迁到 `storyboards/{project_id}/` 时只搬了原图没搬缩略图，资产行的
thumb_path 已指向新目录，文件却还在旧目录 → 前端分镜图卡片全部变成黑色破图，
而进度条仍然显示「已生成 9/9 100%」（数据说完成、画面是坏的）。

用法
----
    python scripts/repair_missing_thumbs.py            # 全部项目
    python scripts/repair_missing_thumbs.py --project prj_xxx
    python scripts/repair_missing_thumbs.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings, stored_path  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import Asset  # noqa: E402
from app.services.thumbnail import make_thumb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="只处理指定项目")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    fixed = missing_src = ok = 0
    try:
        q = db.query(Asset)
        if args.project:
            q = q.filter(Asset.project_id == args.project)
        for a in q.all():
            if not a.file_path:
                continue
            thumb = stored_path(a.thumb_path) if a.thumb_path else None
            if thumb is not None and thumb.exists():
                ok += 1
                continue
            src = stored_path(a.file_path)
            if not src.exists():
                missing_src += 1
                print(f"  ! 原图缺失，跳过 {a.id} {a.file_path}")
                continue
            print(f"  + 重生成缩略图 {a.id} {a.name}")
            if args.dry_run:
                fixed += 1
                continue
            try:
                a.thumb_path = make_thumb(str(src), a.name)
                fixed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ! 失败 {a.id}: {exc}")
        if not args.dry_run:
            db.commit()
    finally:
        db.close()

    print(
        f"\n缩略图正常 {ok}，补齐 {fixed}，原图缺失 {missing_src}"
        + ("  [dry-run 未写库]" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
