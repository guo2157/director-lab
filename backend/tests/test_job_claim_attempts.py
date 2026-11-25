"""任务认领语义回归（D-26）：续跑轮询不得累加 attempts。

真实故障：一张分镜图在远端跑 6 分钟，期间每轮调度都把任务重新认领一次，
attempts 被刷到 130（job_dc070be8b2db）。attempts 是 job_max_attempts 闸口的唯一依据，
被轮询刷高之后：
- 实例整机掉线时的故障转移（attempts < 3）第一轮就判死
- 图片质量不合格的换 seed 重试（job.attempts < job_max_attempts）永远不触发
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'claim.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    s = db_mod.SessionLocal()
    yield s
    s.close()


def _job(db, **kw) -> Job:
    from app.models import Project

    if db.get(Project, "prj_x") is None:
        db.add(Project(id="prj_x", name="认领用例", aspect_ratio="9:16"))
        db.commit()
    job = Job(
        project_id="prj_x",
        shot_id=None,
        job_type="generate_keyframe",
        status=kw.pop("status", "queued"),
        submission_state=kw.pop("submission_state", "not_submitted"),
        input_snapshot=kw.pop("input_snapshot", None),
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        **kw,
    )
    db.add(job)
    db.commit()
    return job


def _claim(db, job) -> bool:
    from app.worker.executor import _claim_job_for_execution

    return _claim_job_for_execution(db, job)


def test_first_claim_increments_attempts(db):
    job = _job(db, attempts=0)
    assert _claim(db, job) is True
    assert job.status == "running"
    assert job.attempts == 1


def test_resume_poll_does_not_inflate_attempts(db):
    """已提交远端 + 续跑标记：反复认领 attempts 必须保持 1。"""
    job = _job(
        db,
        attempts=1,
        submission_state="submitted",
        input_snapshot={"resume_prompt": True},
        prompt_id="pid-1",
    )
    for _ in range(50):
        job.status = "queued"  # 模拟 remote_still_running 分支把任务重新入队
        db.commit()
        assert _claim(db, job) is True
    assert job.attempts == 1, f"续跑轮询把 attempts 刷成了 {job.attempts}"


def test_second_real_attempt_still_counts(db):
    """续跑结束后真的重新提交（resume_prompt 清掉）时必须正常 +1，闸口才有意义。"""
    job = _job(
        db,
        attempts=1,
        submission_state="submitted",
        input_snapshot={"resume_prompt": True},
        prompt_id="pid-1",
    )
    job.status = "queued"
    db.commit()
    _claim(db, job)  # 轮询：不计数
    # 远端返回失败 → 清掉续跑标记、回退为未提交，下一轮是真实重试
    job.status = "queued"
    job.input_snapshot = {}
    job.submission_state = "not_submitted"
    db.commit()
    _claim(db, job)
    assert job.attempts == 2


def test_concurrent_claim_only_one_wins(db):
    job = _job(db, attempts=0)
    first = _claim(db, job)
    # 第二个线程拿到的是同一行（已是 running），条件 UPDATE 必须不命中
    assert first is True
    assert _claim(db, job) is False
    assert job.attempts == 1


def test_resume_poll_keeps_original_started_at(db):
    """续跑轮询不得刷新 started_at：否则耗时统计恒为 3 秒、停滞回收永不成立。"""
    job = _job(db, attempts=0)  # 首次：真实开始
    _claim(db, job)
    first_started = job.started_at
    assert first_started is not None

    # 提交成功 → 进入续跑轮询阶段
    job.status = "queued"
    job.submission_state = "submitted"
    job.input_snapshot = {"resume_prompt": True}
    job.prompt_id = "pid-1"
    db.commit()

    import time as _t

    _t.sleep(0.01)
    for _ in range(5):
        job.status = "queued"
        db.commit()
        _claim(db, job)
    assert job.started_at == first_started, "续跑轮询把 started_at 刷新了"
    assert job.attempts == 1, "续跑轮询把 attempts 刷高了"
