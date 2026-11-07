"""Execution Verifier 单元测试（意图识别改造 Phase 3 收尾）。
覆盖：
- 异步 Job 类结果：job_id 缺失/Job 不存在/状态非法/项目归属不一致/正常通过
- 同步状态变更类结果（不含 job_id）：透传不误报
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project, ids
from app.services.execution_verifier import verify_execution_result, verify_job_submission


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'verifier.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    now = datetime.now()
    project = Project(
        id=ids.project_id(), name="验证项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    other = Project(
        id=ids.project_id(), name="别的项目", aspect_ratio="9:16",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add_all([project, other])
    session.flush()
    job = Job(
        id=ids.job_id(), project_id=project.id, job_type="generate_keyframe",
        status="queued", created_at=now,
    )
    session.add(job)
    session.commit()
    yield session, project.id, other.id, job.id
    session.close()


def test_verify_job_ok(db):
    """正常异步结果：Job 存在且状态合法，验证通过。"""
    session, pid, _oid, jid = db
    r = verify_job_submission(session, {"job_id": jid}, expected_project_id=pid)
    assert r.verified and r.job_status == "queued"


def test_verify_job_id_missing(db):
    """结果缺少 job_id：验证失败（job_id_missing）。"""
    session, _pid, _oid, _jid = db
    r = verify_job_submission(session, {"status": "queued"})
    assert not r.verified and r.reason_code == "job_id_missing"


def test_verify_job_not_found(db):
    """工具返回成功但库中无此 Job：验证失败（job_not_found）。"""
    session, _pid, _oid, _jid = db
    r = verify_job_submission(session, {"job_id": "job_notexist0001"})
    assert not r.verified and r.reason_code == "job_not_found"


def test_verify_job_invalid_status(db):
    """Job 状态非法：验证失败（job_invalid_status）。"""
    session, pid, _oid, jid = db
    job = session.get(Job, jid)
    job.status = "weird_state"
    session.commit()
    r = verify_job_submission(session, {"job_id": jid}, expected_project_id=pid)
    assert not r.verified and r.reason_code == "job_invalid_status"


def test_verify_job_project_mismatch(db):
    """Job 归属项目与预期不一致：验证失败（job_project_mismatch）。"""
    session, pid, oid, jid = db
    r = verify_job_submission(session, {"job_id": jid}, expected_project_id=oid)
    assert not r.verified and r.reason_code == "job_project_mismatch"


def test_sync_result_passthrough(db):
    """同步状态变更类结果（不含 job_id）：透传验证通过，不误报。"""
    session, pid, _oid, _jid = db
    r = verify_execution_result(
        session, {"project_id": pid, "assets_review_status": "approved"},
        expected_project_id=pid,
    )
    assert r.verified


def test_async_result_via_unified_entry(db):
    """统一入口：含 job_id 的异步结果走 Job 验证。"""
    session, pid, _oid, jid = db
    r = verify_execution_result(
        session, {"job_id": jid, "job_status": "queued"}, expected_project_id=pid
    )
    assert r.verified and r.job_id == jid
