"""回归测试：删除作品时必须能连带取消在途任务（force 语义）。

真实事故（2026-11-21，项目「狼来了」）：
用户在作品列表点「彻底删除」，弹窗告诉他有 9 个进行中的任务，
「请先到任务页取消或等待完成后再试」。但任务页入口早就从导航下线了，
于是这个提示把用户堵死——作品删不掉，也没有入口取消任务。

修复后的契约：
- force=false（默认）：行为不变，存在在途任务返回 409（保留给脚本 / 未来入口）
- force=true：服务端先取消项目下全部在途工作（Job + 编排层 Task + AgentTask），
  再执行软删除 / 硬删除；软删除不返回体，硬删除在响应里回执 cancelled 计数

覆盖点：
1. 不带 force 的硬删除仍然 409，且不动数据（防止误删保护被削弱）
2. 带 force 的硬删除：任务被置 cancelled、项目与关联行被清理、响应回执取消数
3. 带 force 的软删除：只写 deleted_at，任务同样被取消
4. 取消范围包含统一任务层（Task），否则 DAG 调度器会继续派发新 Job
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import AgentSession, AgentTask, Base, Job, Project, Task


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """返回 (client, session_factory)。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", session_factory)
    return TestClient(app), session_factory


def _seed(session_factory, *, name="狼来了"):
    db = session_factory()
    p = Project(id=f"prj_{name}", name=name, aspect_ratio="9:16")
    db.add(p)
    db.flush()
    # 一个 running（无 prompt_id：不触发 ComfyUI interrupt），一个 queued，一个链式等待
    jobs = [
        Job(id="job_running", project_id=p.id, job_type="parse_script",
            status="running", started_at=_now(), created_at=_now()),
        Job(id="job_queued", project_id=p.id, job_type="generate_keyframe",
            status="queued", created_at=_now()),
        Job(id="job_chained", project_id=p.id, job_type="generate_clip",
            status="chained_wait", created_at=_now()),
        Job(id="job_done", project_id=p.id, job_type="generate_clip",
            status="succeeded", created_at=_now()),
    ]
    db.add_all(jobs)
    sess = AgentSession(id="ags_1", project_id=p.id, title="会话", created_at=_now())
    db.add(sess)
    db.flush()
    db.add_all([
        Task(id="tsk_running", project_id=p.id, task_type="generate",
             title="批量出图", status="running"),
        Task(id="tsk_done", project_id=p.id, task_type="export",
             title="已导出", status="completed"),
    ])
    db.add(AgentTask(id="agt_1", session_id=sess.id, intent="generate", status="running",
                     created_at=_now()))
    db.commit()
    db.close()
    return p.id


def test_purge_without_force_still_blocked(env):
    client, session_factory = env
    pid = _seed(session_factory)

    r = client.delete(f"/api/projects/{pid}/purge")
    assert r.status_code == 409, r.text

    db = session_factory()
    try:
        assert db.get(Project, pid) is not None
        assert db.get(Job, "job_running").status == "running"
        assert db.get(Job, "job_queued").status == "queued"
    finally:
        db.close()


def test_purge_force_cancels_jobs_then_deletes(env):
    client, session_factory = env
    pid = _seed(session_factory)

    r = client.delete(f"/api/projects/{pid}/purge", params={"force": "true"})
    assert r.status_code == 200, r.text
    body = r.json()

    # 回执：3 个在途任务被取消（succeeded 的那个不算）
    assert body["cancelled"]["jobs"] == 3
    assert body["cancelled"]["tasks"] == 1
    assert body["cancelled"]["agent_tasks"] == 1
    # 项目与其全部关联行都已清除
    assert body["deleted"]["project"] == 1

    db = session_factory()
    try:
        assert db.get(Project, pid) is None
        assert db.query(Job).filter(Job.project_id == pid).count() == 0
        assert db.query(Task).filter(Task.project_id == pid).count() == 0
    finally:
        db.close()


def test_cancel_marks_jobs_and_tasks_before_deletion(env):
    """取消动作本身的口径：跑在删除之前，且三层（Job/Task/AgentTask）都置终态。"""
    client, session_factory = env
    pid = _seed(session_factory, name="游乐场")

    from app.services.project_purge import cancel_project_active_work

    db = session_factory()
    try:
        stats = cancel_project_active_work(db, pid, reason="project_purge")
        assert stats["jobs"] == 3
        assert stats["running"] == 1
        assert stats["tasks"] == 1
        assert stats["agent_tasks"] == 1

        db.expire_all()
        for jid in ("job_running", "job_queued", "job_chained"):
            j = db.get(Job, jid)
            assert j.status == "cancelled"
            assert j.finished_at is not None
            assert j.prompt_id is None
        assert db.get(Job, "job_done").status == "succeeded"  # 终态不被动
        assert db.get(Task, "tsk_running").status == "cancelled"
        assert db.get(Task, "tsk_done").status == "completed"
        assert db.get(AgentTask, "agt_1").status == "cancelled"
        # 取消后不再有阻塞任务，force 删除的预检可放行
        from app.services.project_purge import find_blocking_jobs

        assert find_blocking_jobs(db, pid) == 0
    finally:
        db.close()


def test_soft_delete_force_cancels_and_marks_deleted(env):
    client, session_factory = env
    pid = _seed(session_factory, name="海风与夏日")

    r = client.delete(f"/api/projects/{pid}", params={"force": "true"})
    assert r.status_code == 204, r.text

    db = session_factory()
    try:
        p = db.get(Project, pid)
        assert p is not None
        assert p.deleted_at is not None
        db.expire_all()
        assert db.get(Job, "job_running").status == "cancelled"
        assert db.get(Job, "job_queued").status == "cancelled"
    finally:
        db.close()
