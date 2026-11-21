"""重新解析的前置终止语义（需求：解析中重新解析必须先终止再重发）。

真实故障背景：
- worker 是单线程串行（`_compute_concurrency()` = ComfyUI 实例数，单实例=1）；
- 解析中直接再发一个 force 任务，旧任务仍在跑、继续烧 token，新任务排在后面
  干等，解析页显示「排队中」—— 用户看到的就是「点了重新解析没反应」；
- 前端即使先调 cancel，连点 / 多标签页 / 刷新后重提都能绕过。

所以终止必须在后端做。这组测试锁住：
1. force=true 时，本项目 queued/running 的 script_agent 任务全部置 cancelled
   并写 job.cancelled 事件（reason=superseded_by_reparse）；
2. force=true 时仍正常创建新任务（不被上面的清理误伤）；
3. 非 force 的普通「开始解析」**不**清理在途任务（避免误杀正在跑的批次）；
4. 不碰其他项目、其他类型（如 generate_*）的任务。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Job, JobEvent, Project, ScriptVersion, ids

SCRIPT_TEXT = "第一集。林昭推开门，走廊尽头有一盏灯。\n第二集。灯灭了。"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'reparse.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    session = maker()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    project = Project(
        id=ids.project_id(), name="重解析项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=project.id, version=1,
        content_snapshot=SCRIPT_TEXT,
        content_hash=__import__("hashlib").sha256(SCRIPT_TEXT.encode()).hexdigest(),
        created_at=now,
    )
    session.add(sv)

    # 另一个项目：用于验证不会被误伤
    other = Project(
        id=ids.project_id(), name="别的项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(other)
    other_sv = ScriptVersion(
        id=ids.new_id("scv"), project_id=other.id, version=1,
        content_snapshot=SCRIPT_TEXT,
        content_hash=__import__("hashlib").sha256(SCRIPT_TEXT.encode()).hexdigest(),
        created_at=now,
    )
    session.add(other_sv)
    session.commit()

    payload = {
        "project_id": project.id, "script_version_id": sv.id, "text": SCRIPT_TEXT,
        "other_project_id": other.id,
    }
    session.close()

    yield maker, payload
    engine.dispose()


def _add_job(maker, project_id: str, *, job_type: str = "script_agent",
             status: str = "running") -> str:
    session = maker()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type=job_type,
        params={"script_version_id": "x", "episode_start": 1},
        status=status, created_at=now,
        started_at=now if status == "running" else None,
    )
    session.add(job)
    session.commit()
    jid = job.id
    session.close()
    return jid


def _force_reparse(client: TestClient, project_id: str, sv_id: str) -> "object":
    return client.post(
        f"/api/projects/{project_id}/script-agent",
        json={"script_version_id": sv_id, "text": SCRIPT_TEXT, "force": True},
    )


def test_reparse_cancels_inflight_jobs(env):
    """解析中重新解析：在途任务必须被终止，新任务照常入队。"""
    maker, p = env
    client = TestClient(app)
    running = _add_job(maker, p["project_id"], status="running")
    queued = _add_job(maker, p["project_id"], status="queued")

    r = _force_reparse(client, p["project_id"], p["script_version_id"])
    assert r.status_code == 202, r.text
    new_job_id = r.json()["job_id"]

    session = maker()
    try:
        j1 = session.get(Job, running)
        j2 = session.get(Job, queued)
        assert j1.status == "cancelled", f"running 任务未终止：{j1.status}"
        assert j2.status == "cancelled", f"queued 任务未终止：{j2.status}"
        assert j1.finished_at is not None
        assert j2.finished_at is not None

        ev = (
            session.query(JobEvent)
            .filter(JobEvent.job_id == running,
                    JobEvent.event_type == "job.cancelled")
            .first()
        )
        assert ev is not None, "缺少 job.cancelled 事件"
        assert (ev.payload or {}).get("reason") == "superseded_by_reparse"

        new_job = session.get(Job, new_job_id)
        assert new_job is not None
        assert new_job.status == "queued"
        assert (new_job.params or {}).get("force") is True
    finally:
        session.close()


def test_reparse_leaves_other_projects_and_types_untouched(env):
    """只清理本项目的解析任务，别的项目、别的类型不能被误杀。"""
    maker, p = env
    client = TestClient(app)
    other_proj_job = _add_job(maker, p["other_project_id"], status="running")
    generate_job = _add_job(maker, p["project_id"], job_type="generate_keyframe",
                            status="running")

    r = _force_reparse(client, p["project_id"], p["script_version_id"])
    assert r.status_code == 202

    session = maker()
    try:
        assert session.get(Job, other_proj_job).status == "running"
        assert session.get(Job, generate_job).status == "running"
    finally:
        session.close()


def test_plain_start_does_not_cancel_inflight(env):
    """普通开始解析（force=false）不得清理在途任务 —— 否则批次会被误杀。"""
    maker, p = env
    client = TestClient(app)
    running = _add_job(maker, p["project_id"], status="running")

    r = client.post(
        f"/api/projects/{p['project_id']}/script-agent",
        json={"script_version_id": p["script_version_id"], "text": SCRIPT_TEXT},
    )
    assert r.status_code == 202

    session = maker()
    try:
        assert session.get(Job, running).status == "running", (
            "非 force 的普通解析把在途任务取消了"
        )
    finally:
        session.close()
