"""重建 prj_9db2e016b337 的解析数据。

## 为什么需要

2026-11-21 10:46 我（助手）用 curl 手动触发了一次 confirm，想验证
`pending_persist_episodes` 修复。确认接口会把同一 ParseVersion 下**所有**成功
任务按 episode_start 升序回放，而当时的 `apply_parse_result` 用项目全部场景作为
删除范围 → 回放第 1 集时删掉了后续任务才重建的镜头。

实测后果：场景 4 → 12（重复创建）、镜头 17 → 39（数量对但内容错乱，
同名场景被拆成多条记录）。

## 重建策略

按集取**最后一次成功**任务的结果落库：

    第 1 集 → job_24846c12f34f (2026-11-20 23:50, 14 镜)  ← 该集最晚一次
    第 2 集 → job_619c682d5844 (2026-11-21 01:54, 11 镜)  ← 唯一一次

只回放这两个任务，不碰更早的中间态（那些是用户反复重解析的过程产物）。
先清空该项目的场景/镜头/场景-集关联，再从零重建，避免残留重复场景。

用法：
    cd backend && .venv/bin/python _rebuild_prj_9db2.py --dry-run
    cd backend && .venv/bin/python _rebuild_prj_9db2.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from app.core.database import SessionLocal
from app.models import (
    AssetRef, Job, ParseVersion, Project, PropShotState, Scene, SceneEpisode, Shot,
)
from app.models.audio import AudioSegment

PROJECT_ID = "prj_9db2e016b337"
# 每集取最后一次成功任务（按 finished_at 最晚）
REPLAY_JOB_IDS = [
    "job_24846c12f34f",  # 第 1 集，14 镜
    "job_619c682d5844",  # 第 2 集，11 镜
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入；默认只报告")
    ap.add_argument("--dry-run", action="store_true", help="只报告（默认行为）")
    args = ap.parse_args()
    dry = not args.apply

    db = SessionLocal()
    try:
        project = db.get(Project, PROJECT_ID)
        if project is None:
            print(f"项目 {PROJECT_ID} 不存在")
            return 1

        # ---- 1. 现状 ----
        scene_ids = [r[0] for r in db.query(Scene.id).filter(
            Scene.project_id == PROJECT_ID).all()]
        shot_count = (db.query(Shot).filter(Shot.scene_id.in_(scene_ids)).count()
                      if scene_ids else 0)
        print(f"重建前：场景 {len(scene_ids)} 个，镜头 {shot_count} 个")

        # ---- 2. 取要回放的结果 ----
        payloads = []
        for jid in REPLAY_JOB_IDS:
            job = db.get(Job, jid)
            if job is None or not job.result:
                print(f"任务 {jid} 不存在或无 result")
                return 1
            res = job.result
            eps = sorted({int(s.get("episode") or 0) for s in (res.get("scenes") or [])})
            shots = sum(len(s.get("shots") or []) for s in (res.get("scenes") or []))
            print(f"  回放 {jid}: 集 {eps}，场景 {len(res.get('scenes') or [])} 个，"
                  f"镜头 {shots} 个，episode_start={res.get('episode_start')}")
            payloads.append(res)

        total_expected = sum(
            sum(len(s.get("shots") or []) for s in (p.get("scenes") or []))
            for p in payloads)
        print(f"预期重建后：镜头 {total_expected} 个")

        if dry:
            print("\n[dry-run] 未写入。加 --apply 执行。")
            return 0

        # ---- 3. 清空该项目的解析产物 ----
        # 只删分镜层的场景/镜头/关联，不动角色与道具（项目级资产，跨集复用）
        if scene_ids:
            db.query(Job).filter(Job.shot_id.in_(
                db.query(Shot.id).filter(Shot.scene_id.in_(scene_ids))
            )).update({"shot_id": None}, synchronize_session=False)
            db.query(AssetRef).filter(AssetRef.shot_id.in_(
                db.query(Shot.id).filter(Shot.scene_id.in_(scene_ids))
            )).delete(synchronize_session=False)
            db.query(PropShotState).filter(PropShotState.shot_id.in_(
                db.query(Shot.id).filter(Shot.scene_id.in_(scene_ids))
            )).delete(synchronize_session=False)
            db.query(AudioSegment).filter(AudioSegment.shot_id.in_(
                db.query(Shot.id).filter(Shot.scene_id.in_(scene_ids))
            )).delete(synchronize_session=False)
            db.query(Shot).filter(Shot.scene_id.in_(scene_ids)).delete(
                synchronize_session=False)
            db.query(SceneEpisode).filter(SceneEpisode.scene_id.in_(scene_ids)).delete(
                synchronize_session=False)
            db.query(Scene).filter(Scene.project_id == PROJECT_ID).delete(
                synchronize_session=False)
        db.flush()
        print("已清空旧场景/镜头")

        # ---- 4. 逐集回放 ----
        # current_scene_ids 传空列表：清空后没有「既有场景」需要保护，
        # 且修复后的删除范围只认本次 parse_result 里的场景，不会误删。
        from app.services.script_parse import apply_parse_result
        for res in payloads:
            stats = apply_parse_result(
                db, project, res,
                episode_start=int(res.get("episode_start") or 1),
                episode_count=int(res.get("episode_count") or 1),
                current_scene_ids=[],
            )
            print(f"  落库：{stats}")
        db.commit()

        # ---- 5. 校验 ----
        scene_ids2 = [r[0] for r in db.query(Scene.id).filter(
            Scene.project_id == PROJECT_ID).all()]
        print(f"\n重建后：场景 {len(scene_ids2)} 个")
        for sc in db.query(Scene).filter(Scene.project_id == PROJECT_ID).order_by(Scene.seq):
            n = db.query(Shot).filter(Shot.scene_id == sc.id).count()
            eps = [r[0] for r in db.query(SceneEpisode.episode)
                   .filter(SceneEpisode.scene_id == sc.id).order_by(SceneEpisode.episode)]
            print(f"  #{sc.seq} {sc.title}  集={eps}  镜头={n}")
        total = (db.query(Shot).filter(Shot.scene_id.in_(scene_ids2)).count()
                 if scene_ids2 else 0)
        print(f"\n总镜头 {total} 个（预期 {total_expected}）")
        if total != total_expected:
            print("!! 镜头数与预期不符，请检查")
            return 1
        print("✓ 重建完成")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
