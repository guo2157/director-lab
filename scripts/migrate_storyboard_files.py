#!/usr/bin/env python
"""把平铺的分镜产物迁移到「按项目分目录」的新口径。

背景
----
`storyboard_{项目内全局镜号}_img|clip` 曾平铺写在 `media/storyboards/` 下，
而镜号是项目内序号 → 不同项目写同一个文件名，后生成的项目静默覆盖前一个项目的
分镜图（实测 3 个项目的 storyboard_1_img 资产行指向同一文件）。
落盘代码已改为 `storyboards/{project_id}/`，本脚本负责把历史数据迁过去。

用法
----
    python scripts/migrate_storyboard_files.py --project prj_xxx          # 只迁一个项目
    python scripts/migrate_storyboard_files.py --all-projects --dry-run   # 看影响面

对每个命中的资产行：
1. 目标目录 `media/storyboards/{project_id}/` 不存在同名的才复制（不覆盖已有新图）
2. 资产行 file_path / thumb_path 改指新路径
3. 旧平铺文件**不删除**：可能仍被其它项目引用（跨项目共享），删了会让那些项目丢图

注意：若同一平铺文件被多个项目共享，说明历史内容已被相互覆盖，
迁移只能保证「今后不再互相覆盖」，无法恢复已被覆盖的旧图。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
from app.models import Asset  # noqa: E402


def _media_root() -> Path:
    return Path(get_settings().media_dir)


def _resolve_src(rel: str) -> Path:
    """资产行的 file_path 有两种历史写法，按存在性依次尝试。

    - `data/media/storyboards/x.png`（相对 backend 工作目录，主流写法）
    - `storyboards/x.png`（相对 media_dir）
    """
    p = Path(rel)
    if p.is_absolute():
        return p
    for cand in (p, _media_root() / p):
        if cand.exists():
            return cand
    return p


def _is_flat_storyboard(rel: str | None) -> bool:
    if not rel:
        return False
    p = rel.replace("\\", "/")
    if "/storyboards/" not in p:
        return False
    tail = p.rsplit("/", 1)[-1]
    # 新口径是 storyboards/{prj_xxx}/xxx.png，旧口径是 storyboards/xxx.png
    return tail.startswith("storyboard_") and not p.rsplit("/", 2)[-2].startswith("prj_")


def migrate(db, project_id: str | None, dry_run: bool) -> dict:
    q = db.query(Asset).filter(Asset.asset_type.in_(("keyframe", "clip")))
    if project_id:
        q = q.filter(Asset.project_id == project_id)
    rows = q.all()

    stats = {"scanned": 0, "migrated": 0, "skipped_exists": 0, "missing_file": 0, "projects": set()}
    for a in rows:
        if not _is_flat_storyboard(a.file_path):
            continue
        stats["scanned"] += 1
        src = _resolve_src(a.file_path)
        dest_dir = _media_root() / "storyboards" / a.project_id
        dest = dest_dir / Path(a.file_path).name
        # 资产行里存的是相对 backend 工作目录的路径（data/media/...），
        # 这里按同样风格回写，避免绝对路径污染数据。
        rel_dest = Path(a.file_path).parent / a.project_id / Path(a.file_path).name

        if not src.exists():
            stats["missing_file"] += 1
            print(f"  ! 源文件不存在，跳过 {a.id} {a.file_path}")
            continue
        if dest.exists():
            stats["skipped_exists"] += 1
            print(f"  = 目标已存在，仅改指向 {a.id} -> {rel_dest}")
        else:
            print(f"  -> {a.id} {a.file_path} => {rel_dest}")
            if not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)

        if not dry_run:
            a.file_path = str(rel_dest)
            thumb = getattr(a, "thumb_path", None)
            if thumb and Path(thumb).name.startswith(src.stem):
                a.thumb_path = str(Path(rel_dest).parent / Path(thumb).name)
        stats["migrated"] += 1
        stats["projects"].add(a.project_id)

    if not dry_run:
        db.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="只处理指定项目 id")
    ap.add_argument("--all-projects", action="store_true", help="处理全部项目")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.project and not args.all_projects:
        ap.error("必须指定 --project 或 --all-projects")

    db = SessionLocal()
    try:
        stats = migrate(db, args.project, args.dry_run)
    finally:
        db.close()

    print(
        f"\n扫描 {stats['scanned']} 行，迁移 {stats['migrated']} 行"
        f"（目标已存在 {stats['skipped_exists']}，源缺失 {stats['missing_file']}），"
        f"涉及项目 {len(stats['projects'])} 个"
        + ("  [dry-run 未写库]" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
