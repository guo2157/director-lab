"""Agent 受控工具注册表测试（阶段 E 首批只读工具）。

覆盖：项目概览/任务列表/资产统计的真实查询、未知工具拒绝、
不存在项目的错误分类、参数错误分类。
"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project
from app.models.ids import job_id, project_id
from app.services.agent_tools import TOOL_REGISTRY, execute_tool


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    from datetime import datetime
    session = factory()
    # 造一个真实项目与两条任务
    p = Project(
        id=project_id(), name="工具测试项目", aspect_ratio="16:9",
        creation_type="script", status="active", created_at=datetime.now(), updated_at=datetime.now(),
    )
    session.add(p)
    session.add(Job(id=job_id(), project_id=p.id, job_type="image", status="succeeded", created_at=datetime.now()))
    session.add(Job(id=job_id(), project_id=p.id, job_type="clip", status="failed", error_code="comfy_unreachable", created_at=datetime.now()))
    session.commit()
    yield session, p.id
    session.close()


def test_registry_contains_readonly_tools():
    assert set(TOOL_REGISTRY) >= {"project_overview", "list_jobs", "asset_stats"}


def test_project_overview(db):
    session, pid = db
    r = execute_tool(session, "project_overview", project_id=pid)
    assert r.ok, r.error_message
    assert r.data["name"] == "工具测试项目"
    assert r.data["status"] == "active"


def test_list_jobs(db):
    session, pid = db
    r = execute_tool(session, "list_jobs", project_id=pid)
    assert r.ok
    assert r.data["count"] == 2
    # 状态过滤
    r = execute_tool(session, "list_jobs", project_id=pid, status="failed")
    assert r.ok
    assert r.data["count"] == 1
    assert r.data["items"][0]["error_code"] == "comfy_unreachable"


def test_asset_stats(db):
    session, pid = db
    r = execute_tool(session, "asset_stats", project_id=pid)
    assert r.ok
    assert r.data["total"] == 0


def test_unknown_tool_rejected(db):
    session, _ = db
    r = execute_tool(session, "not_a_tool")
    assert not r.ok
    assert r.error_code == "tool_not_found"


def test_project_not_found(db):
    session, _ = db
    r = execute_tool(session, "project_overview", project_id="prj_notexist")
    assert not r.ok
    assert r.error_code == "project_not_found"


def test_invalid_args(db):
    session, _ = db
    r = execute_tool(session, "project_overview")
    assert not r.ok
    assert r.error_code == "tool_invalid_args"
