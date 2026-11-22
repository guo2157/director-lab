"""回归测试：分镜页「取消在途」批量取消接口。

背景（2026-11-21）：任务中心页面已下线，分镜图 / 分镜看板在批量生成后没有任何
取消入口。用户批量出图选错工作流、链式批量排了 8 镜视频，只能干等跑完。

接口：POST /api/jobs/projects/{id}/cancel?target=keyframe|clip|audio|all[&shot_ids=]
契约：
1. 只取消目标 stage（job_type）的在途任务，不误伤其它阶段
2. chained_wait / submission_unknown 与 queued / running 一并算在途
3. 终态任务（succeeded / failed）不动
4. 取消后上游编排任务必须收尾：否则 bridge 会立刻派发 pending 阶段，
   新 Job 重新排队，用户会觉得「点了取消没用」
5. 未知 target → 400
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Job, Project, Task, TaskStage


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    return TestClient(app), factory


def _seed(factory, *, name="狼来了"):
    db = factory()
    p = Project(id=f"prj_{name}", name=name, aspect_ratio="9:16")
    db.add(p)
    db.flush()
    rows = [
        # 分镜图批次：1 running + 1 queued
        ("job_kf_run", "generate_keyframe", "running"),
        ("job_kf_q", "generate_keyframe", "queued"),
        # 视频批次：链式一条在跑、一条等前序
        ("job_clip_run", "generate_clip", "running"),
        ("job_clip_chain", "generate_clip", "chained_wait"),
        # 已完成的出图任务不应被动
        ("job_kf_done", "generate_keyframe", "succeeded"),
    ]
    for jid, jtype, st in rows:
        db.add(Job(id=jid, project_id=p.id, job_type=jtype, status=st, created_at=_now()))
    db.flush()
    # 一个批次任务，阶段挂在 running 的出图 Job 上，另有一个尚未派发的 pending 阶段
    db.add(Task(id="tsk_batch", project_id=p.id, task_type="generate", title="批量出图", status="running"))
    db.flush()
    db.add_all([
        TaskStage(id="stg_run", task_id="tsk_batch", stage_key="shot_1", stage_index=1,
                  stage_name="出图", status="running", job_id="job_kf_run", created_at=_now()),
        TaskStage(id="stg_pending", task_id="tsk_batch", stage_key="shot_2", stage_index=2,
                  stage_name="出图", status="pending", created_at=_now()),
    ])
    db.commit()
    db.close()
    return p.id


def test_cancel_keyframe_only_touches_keyframe(env):
    client, factory = env
    pid = _seed(factory, name="只取消出图")

    r = client.post(f"/api/jobs/projects/{pid}/cancel", params={"target": "keyframe"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs"] == 2  # running + queued
    assert body["running"] == 1
    assert body["interrupted"] == 0  # prompt_id 为空，未调用 ComfyUI

    db = factory()
    try:
        assert db.get(Job, "job_kf_run").status == "cancelled"
        assert db.get(Job, "job_kf_q").status == "cancelled"
        assert db.get(Job, "job_kf_done").status == "succeeded"
        # 视频任务不被牵连
        assert db.get(Job, "job_clip_run").status == "running"
        assert db.get(Job, "job_clip_chain").status == "chained_wait"
    finally:
        db.close()


def test_cancel_clip_includes_chained_wait(env):
    client, factory = env
    pid = _seed(factory, name="取消视频")

    r = client.post(f"/api/jobs/projects/{pid}/cancel", params={"target": "clip"})
    assert r.status_code == 200
    assert r.json()["jobs"] == 2

    db = factory()
    try:
        assert db.get(Job, "job_clip_run").status == "cancelled"
        assert db.get(Job, "job_clip_chain").status == "cancelled"
        assert db.get(Job, "job_kf_run").status == "running"
    finally:
        db.close()


def test_cancel_closes_orphan_task_and_blocks_new_dispatch(env):
    """上游任务必须收尾：否则 bridge 会把 pending 阶段立刻派发出去。"""
    client, factory = env
    pid = _seed(factory, name="任务收尾")

    r = client.post(f"/api/jobs/projects/{pid}/cancel", params={"target": "keyframe"})
    assert r.status_code == 200
    assert r.json()["tasks"] == 1

    db = factory()
    try:
        task = db.get(Task, "tsk_batch")
        assert task.status == "cancelled"
        assert db.get(TaskStage, "stg_run").status == "cancelled"
        # 未派发的 pending 阶段被取消，不能再被调度出去
        assert db.get(TaskStage, "stg_pending").status == "cancelled"
    finally:
        db.close()


def test_cancel_with_shot_filter(env):
    client, factory = env
    pid = _seed(factory, name="按镜头取消")
    db = factory()
    try:
        db.get(Job, "job_kf_run").shot_id = None  # 无 shot_id 的任务不应被 shot 过滤命中
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/jobs/projects/{pid}/cancel",
                    params={"target": "keyframe", "shot_ids": "shot_none"})
    assert r.status_code == 200
    assert r.json()["jobs"] == 0

    db = factory()
    try:
        assert db.get(Job, "job_kf_run").status == "running"
    finally:
        db.close()


def test_cancel_unknown_target_rejected(env):
    client, factory = env
    pid = _seed(factory, name="非法target")
    r = client.post(f"/api/jobs/projects/{pid}/cancel", params={"target": "bogus"})
    assert r.status_code == 400, r.text
