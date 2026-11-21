"""跨进程取消信号：DB 轮询层（本模块最重要的回归点）。

真实部署是**两个 systemd 进程**：

    director-lab-backend.service  → uvicorn（处理 POST /jobs/{id}/cancel）
    director-lab-worker.service   → python -m app.worker.scheduler（跑解析线程）

模块级 set 不跨进程共享，所以 `is_cancelled()` 必须在内存未命中时查库。
这组测试用**独立的 DB 会话**模拟「另一个进程改了状态」，锁住三件事：

1. 别的进程把 job.status 改成 cancelled 后，worker 侧轮询能得到 True；
2. 轮询是节流的：两次调用之间不会每次都打 DB（否则每行 SSE 一次查询会拖垮 SQLite）；
3. 缓存活不过阈值：状态改完后，超过 _POLL_INTERVAL 必然被看见；
4. clear() 会清掉缓存，避免同一 job_id 复跑被上次的 True 误判取消。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, ids
from app.services import job_cancel


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'cancel.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session = maker()
    project = Project(
        id=ids.project_id(), name="取消测试", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(project)
    job = Job(
        id=ids.job_id(), project_id=project.id, job_type="script_agent",
        params={}, status="running", created_at=now, started_at=now,
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    job_cancel.clear(job_id)
    yield maker, job_id
    job_cancel.clear(job_id)
    engine.dispose()


def _set_status(maker, job_id: str, status: str) -> None:
    """模拟另一个进程（API）写状态。"""
    session = maker()
    try:
        session.get(Job, job_id).status = status
        session.commit()
    finally:
        session.close()


def test_cancel_from_other_process_is_detected(env, monkeypatch):
    """别的进程把 status 改成 cancelled → worker 轮询必须看见。"""
    maker, job_id = env
    # 关掉节流，保证第一次查询就落地（真实值 0.5s，测试里没必要等）
    monkeypatch.setattr(job_cancel, "_POLL_INTERVAL", 0.0)

    assert job_cancel.is_cancelled(job_id) is False, "初始状态不应是已取消"

    _set_status(maker, job_id, "cancelled")

    assert job_cancel.is_cancelled(job_id) is True, (
        "另一个进程写了 cancelled，worker 侧没读到 —— 跨进程取消失效"
    )


def test_polling_is_throttled(env, monkeypatch):
    """节流生效：阈值内的重复调用不查库（热路径每行 SSE 都会调）。"""
    maker, job_id = env
    monkeypatch.setattr(job_cancel, "_POLL_INTERVAL", 10.0)

    assert job_cancel.is_cancelled(job_id) is False

    calls = {"n": 0}
    real = job_cancel._read_db_status

    def _counting(jid):
        calls["n"] += 1
        return real(jid)

    monkeypatch.setattr(job_cancel, "_read_db_status", _counting)
    # 阈值内连查 50 次：一次都不该打 DB
    for _ in range(50):
        job_cancel.is_cancelled(job_id)
    assert calls["n"] == 0, f"节流失效，{calls['n']} 次调用都查了库"


def test_cache_expires_and_picks_up_cancel(env, monkeypatch):
    """阈值过后必须重新查库：先读到的 False 不能被永久缓存。"""
    maker, job_id = env
    monkeypatch.setattr(job_cancel, "_POLL_INTERVAL", 0.05)

    assert job_cancel.is_cancelled(job_id) is False
    _set_status(maker, job_id, "cancelled")
    time.sleep(0.12)
    assert job_cancel.is_cancelled(job_id) is True, "缓存过期后没重新查库"


def test_clear_resets_state_for_reused_job_id(env, monkeypatch):
    """clear() 必须同时清 set 与缓存：否则复跑会被上次的 True 误判取消。"""
    maker, job_id = env
    monkeypatch.setattr(job_cancel, "_POLL_INTERVAL", 0.0)

    _set_status(maker, job_id, "cancelled")
    assert job_cancel.is_cancelled(job_id) is True

    # 复跑：状态回到 running，clear 后必须能重新工作
    _set_status(maker, job_id, "running")
    job_cancel.clear(job_id)
    assert job_cancel.is_cancelled(job_id) is False


def test_abort_hook_fires_on_request():
    """request() 必须立刻触发已登记的中断回调（close 在途 HTTP 流）。"""
    fired = {"n": 0}

    def _hook():
        fired["n"] += 1

    jid = "job_abort_probe"
    job_cancel.clear(jid)
    job_cancel.register_abort(jid, _hook)
    job_cancel.request(jid)

    assert fired["n"] == 1, "取消没有立刻掐断在途连接（LLM 资源仍在占用）"
    # 取出即清：不能重复回调
    job_cancel.request(jid)
    assert fired["n"] == 1
    job_cancel.clear(jid)


def test_unregister_abort_prevents_late_close():
    """流正常结束后注销：之后到来的取消不能再关这个（已复用的）连接。"""
    fired = {"n": 0}

    def _hook():
        fired["n"] += 1

    jid = "job_abort_probe2"
    job_cancel.clear(jid)
    job_cancel.register_abort(jid, _hook)
    job_cancel.unregister_abort(jid, _hook)
    job_cancel.request(jid)

    assert fired["n"] == 0, "已注销的回调仍被触发，会误关后续请求"
    job_cancel.clear(jid)
