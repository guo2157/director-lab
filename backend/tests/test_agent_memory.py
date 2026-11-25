"""记忆系统测试（记忆系统迭代一）：写入/查询/替代/上下文注入。"""
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.services import agent_memory as memory_svc
from app.services.agent_tools import TOOL_REGISTRY, execute_tool


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    from app.models import Base

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _add(db, content, memory_type="project", project_id=None, source="user"):
    return memory_svc.add_memory(
        db, content=content, memory_type=memory_type,
        project_id=project_id, source=source,
    )


def test_add_and_list_memory(db):
    mem = _add(db, "本项目角色图统一使用写实风格")
    assert mem.id.startswith("amr_")
    assert mem.memory_type == "project"
    assert mem.status == "active"
    rows = memory_svc.list_memories(db, project_id=None)
    assert any(m.id == mem.id for m in rows)


def test_add_memory_type_whitelist(db):
    with pytest.raises(ValueError):
        _add(db, "x", memory_type="invalid_type")
    with pytest.raises(ValueError):
        _add(db, "   ")


def test_list_filters_by_project_and_type(db):
    _add(db, "项目规范A", memory_type="project", project_id="prj_1")
    _add(db, "全局偏好B", memory_type="preference", project_id=None)
    _add(db, "经验C", memory_type="experience", project_id="prj_2")
    rows = memory_svc.list_memories(db, project_id="prj_1")
    contents = [m.content for m in rows]
    assert "项目规范A" in contents
    assert "全局偏好B" in contents  # 全局偏好始终包含
    assert "经验C" not in contents  # 其他项目记忆不包含
    only_pref = memory_svc.list_memories(db, memory_type="preference")
    assert all(m.memory_type == "preference" for m in only_pref)


def test_supersede_memory(db):
    old = _add(db, "旧规范")
    new_mem = memory_svc.supersede_memory(db, old.id, new_content="新规范")
    db.flush()
    assert old.status == "superseded"
    assert old.superseded_by == new_mem.id
    assert new_mem.content == "新规范"
    # 已 superseded 的记忆不可再次替代
    with pytest.raises(ValueError):
        memory_svc.supersede_memory(db, old.id)
    # 默认查询不含 superseded
    rows = memory_svc.list_memories(db, project_id=None)
    assert all(m.id != old.id for m in rows)
    # include_superseded 时可见
    rows_all = memory_svc.list_memories(
        db, project_id=None, include_superseded=True
    )
    assert any(m.id == old.id for m in rows_all)


def test_build_memory_context(db):
    assert memory_svc.build_memory_context(db, None) is None
    _add(db, "角色图必须单人")
    ctx = memory_svc.build_memory_context(db, None)
    assert ctx is not None
    assert "角色图必须单人" in ctx
    assert "以用户当前指令为准" in ctx


def test_memory_tools_registered_and_executable(db):
    for name in ("list_memories", "add_memory", "supersede_memory"):
        assert name in TOOL_REGISTRY
    r = execute_tool(db, "add_memory", content="测试记忆条目")
    assert r.ok, r.error_message
    mem_id = r.data["memory_id"]
    r2 = execute_tool(db, "list_memories")
    assert r2.ok and any(m["id"] == mem_id for m in r2.data["memories"])
    r3 = execute_tool(db, "supersede_memory", memory_id=mem_id)
    assert r3.ok and r3.data["superseded_id"] == mem_id
    # 非法类型经 Schema/服务校验拒绝
    r4 = execute_tool(db, "add_memory", content="x", memory_type="bad")
    assert not r4.ok


def test_global_session_isolated_from_project_memories(db):
    """P0-1 记忆隔离（2026-11-25）：Global 会话（project_id=None）只能读全局记忆，
    不得返回任何项目记忆——此前无过滤，全库项目记忆会泄漏进无项目会话。"""
    _add(db, "项目A角色设定", memory_type="project", project_id="prj_A")
    _add(db, "项目B场景设定", memory_type="project", project_id="prj_B")
    _add(db, "用户喜欢竖屏", memory_type="preference", project_id=None)

    rows = memory_svc.list_memories(db, project_id=None)
    contents = [m.content for m in rows]
    assert "用户喜欢竖屏" in contents, "全局偏好应对 Global 会话可见"
    assert "项目A角色设定" not in contents, "Global 会话不得读取项目A记忆"
    assert "项目B场景设定" not in contents, "Global 会话不得读取项目B记忆"
    # 所有返回条目的 project_id 必须为 None（纯全局记忆）
    assert all(m.project_id is None for m in rows)

    # build_memory_context 注入同样不得泄漏
    ctx = memory_svc.build_memory_context(db, None)
    assert ctx is not None
    assert "用户喜欢竖屏" in ctx
    assert "项目A角色设定" not in ctx
    assert "项目B场景设定" not in ctx


def test_project_sessions_isolated_from_each_other(db):
    """P0-1 记忆隔离（2026-11-25）：项目A会话不得读取项目B记忆，反之亦然。"""
    _add(db, "项目A角色设定", memory_type="project", project_id="prj_A")
    _add(db, "项目B场景设定", memory_type="project", project_id="prj_B")
    _add(db, "全局偏好", memory_type="preference", project_id=None)

    rows_a = memory_svc.list_memories(db, project_id="prj_A")
    contents_a = [m.content for m in rows_a]
    assert "项目A角色设定" in contents_a
    assert "全局偏好" in contents_a, "项目会话应包含全局偏好"
    assert "项目B场景设定" not in contents_a, "项目A会话不得读取项目B记忆"

    rows_b = memory_svc.list_memories(db, project_id="prj_B")
    contents_b = [m.content for m in rows_b]
    assert "项目B场景设定" in contents_b
    assert "全局偏好" in contents_b
    assert "项目A角色设定" not in contents_b, "项目B会话不得读取项目A记忆"
