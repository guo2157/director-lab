"""Agent Runtime 测试夹具（Phase A A12a）。

Runtime 相关测试一律使用这里的 tmp 库夹具，禁止连接 backend/data 下的真实库。
A1/A2 会用到 patch_runtime_sessionmaker 把 SessionLocal 指向临时库。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset,
    Base,
    Job,
    ParseVersion,
    Project,
    ScriptVersion,
    ids,
)


@pytest.fixture()
def rt_engine(tmp_path):
    """Runtime 专用临时库引擎（表结构由 Base.metadata 直接建）。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'rt.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def rt_sessionmaker(rt_engine):
    return sessionmaker(bind=rt_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(rt_sessionmaker):
    session = rt_sessionmaker()
    yield session
    session.close()


@pytest.fixture()
def fresh_session(rt_sessionmaker):
    """返回工厂函数：用于「新开连接验证是否真的落库」。"""
    created: list = []

    def _factory():
        s = rt_sessionmaker()
        created.append(s)
        return s

    yield _factory
    for s in created:
        s.close()


@pytest.fixture()
def patch_runtime_sessionmaker(monkeypatch, rt_engine, rt_sessionmaker):
    """把模块级 SessionLocal / engine 指向临时库。

    agent_runtime 等模块内部用的是 `from app.core.database import SessionLocal`
    或运行时再 import，这里同时替换 database 模块属性与已导入的引用。
    """
    import app.core.database as database_mod

    monkeypatch.setattr(database_mod, "engine", rt_engine)
    monkeypatch.setattr(database_mod, "SessionLocal", rt_sessionmaker)

    for mod_name in ("app.services.agent_runtime", "app.api.v1.agent_loop"):
        try:
            mod = __import__(mod_name, fromlist=["SessionLocal"])
        except ImportError:  # pragma: no cover - 模块不存在时跳过
            continue
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", rt_sessionmaker)
    return rt_sessionmaker


def mk_project(
    db,
    *,
    with_script: bool = False,
    with_parse: bool = False,
    with_assets: int = 0,
) -> Project:
    """构造项目（可带剧本 / 解析 / N 个资产），满足不同前置条件组合。"""
    now = datetime.now()
    project = Project(
        id=ids.project_id(),
        name="Runtime 测试项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    db.flush()

    if with_script:
        sv = ScriptVersion(
            id=ids.script_version_id(),
            project_id=project.id,
            version=1,
            content_snapshot="测试剧本内容",
            content_hash="hash-v1",
            created_at=now,
        )
        db.add(sv)
        db.flush()
        project.current_script_version_id = sv.id

    if with_parse:
        pv = ParseVersion(
            id=ids.parse_version_id(),
            project_id=project.id,
            script_version_id=project.current_script_version_id or ids.script_version_id(),
            version=1,
            status="current",
            created_at=now,
        )
        db.add(pv)
        db.flush()
        project.current_parse_version_id = pv.id

    for i in range(with_assets):
        db.add(
            Asset(
                id=ids.asset_id(),
                project_id=project.id,
                asset_type="character",
                name=f"角色{i + 1}",
                version=1,
                validity="current",
                file_path="placeholder.png",
            )
        )
    db.flush()
    return project


def mk_job(db, project_id: str, *, status: str = "queued", job_type: str = "generate_keyframe") -> Job:
    """创建一条真实 Job 记录（供 Verifier 校验等待态使用）。"""
    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        job_type=job_type,
        status=status,
        params={},
        created_at=datetime.now(),
    )
    db.add(job)
    db.flush()
    return job


def fake_tool(monkeypatch, name: str, *, returns=None, raises=None, spec_overrides=None) -> dict:
    """临时注册/替换一个工具（monkeypatch.setitem 自动还原），返回注册的 spec。

    - returns：工具返回的数据（dict），或接收 kwargs 的可调用对象
    - raises：工具抛出的异常实例
    - spec_overrides：覆盖注册表 spec 的字段（如 requires_approval=True）
    """
    from app.services import agent_tools

    def _fn(_db, **kwargs):
        if raises is not None:
            raise raises
        if callable(returns):
            return returns(**kwargs)
        return returns if returns is not None else {"ok": 1, "args": kwargs}

    spec = {
        "description": f"测试工具 {name}",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": None,
        "preconditions": [],
        "params": {},
        "fn": _fn,
    }
    if spec_overrides:
        spec.update(spec_overrides)

    monkeypatch.setitem(agent_tools.TOOL_REGISTRY, name, spec)
    return spec
