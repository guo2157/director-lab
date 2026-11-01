"""一次性对账：把已具备必需产物、却停在 pending 的镜头修正为 done。

背景：shot_state 口径修正前，对白/旁白镜头即使已有 H3 VA 内嵌音轨，
也会因缺少独立 audio 资产而长期停在 pending —— 合成只收 done/review 镜头，
这些镜头永远进不了成片，字幕也就永远为空。

用法（在 backend 目录执行）：
    .venv/bin/python scripts/reconcile_shot_status.py           # 只预览，不写库
    .venv/bin/python scripts/reconcile_shot_status.py --apply   # 落库

只处理 pending 镜头；generating/review/redo 一律不动。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.models import Shot  # noqa: E402
from app.services.shot_state import shot_ready  # noqa: E402


def main(apply: bool) -> int:
    db = SessionLocal()
    try:
        shots = db.query(Shot).filter(Shot.status == "pending").all()
        ready = [sh for sh in shots if shot_ready(db, sh)]
        print(f"待检查 pending 镜头：{len(shots)}，可置 done：{len(ready)}")
        for sh in ready:
            text = (sh.dialogue or "").replace("\n", " / ")[:32]
            print(f"  {sh.id} ep{sh.episode} seq{sh.seq} {sh.shot_kind:<9} "
                  f"audio={sh.audio_source or '-':<8} {text}")
        if not apply:
            print("\n预览模式：未写库。确认无误后加 --apply 执行。")
            return 0
        for sh in ready:
            sh.status = "done"
        db.commit()
        print(f"\n已更新 {len(ready)} 条镜头状态为 done。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv[1:]))
