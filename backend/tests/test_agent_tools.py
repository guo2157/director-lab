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
    assert set(TOOL_REGISTRY) >= {"project_overview", "list_jobs", "asset_stats", "list_assets"}
    # 只读工具一律无副作用、无需审批
    for name in ("project_overview", "list_jobs", "asset_stats", "list_assets"):
        spec = TOOL_REGISTRY[name]
        assert spec["permission"] == "read"
        assert spec["side_effect"] is False
        assert spec["requires_approval"] is False
        assert spec["input_model"] is not None


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


# --- list_assets 回归：排序 / 分页 / 过滤 / 边界 / 不泄露路径 ---
@pytest.fixture()
def db_assets(tmp_path, monkeypatch):
    """两个关联任务的资产（新/旧）+ 一个无关联任务的资产 + 一个 stale 资产。"""
    from datetime import datetime, timedelta
    from app.models import Asset
    from app.models.ids import asset_id
    db_file = tmp_path / "assets.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    now = datetime.now()
    p = Project(
        id=project_id(), name="资产排序项目", aspect_ratio="16:9",
        creation_type="script", status="active", created_at=now, updated_at=now,
    )
    session.add(p)
    j_old = Job(id=job_id(), project_id=p.id, job_type="image", status="succeeded", created_at=now - timedelta(hours=2))
    j_new = Job(id=job_id(), project_id=p.id, job_type="image", status="succeeded", created_at=now)
    session.add_all([j_old, j_new])
    a_old = Asset(
        id=asset_id(), project_id=p.id, asset_type="character", name="妈妈", version=1,
        source_job_id=j_old.id, file_path="/tmp/old.png", thumb_path="/tmp/old.webp", validity="current",
    )
    a_new = Asset(
        id=asset_id(), project_id=p.id, asset_type="keyframe", name="cover_new", version=1,
        source_job_id=j_new.id, file_path="/tmp/new.png", thumb_path=None, validity="current",
    )
    a_none = Asset(
        id=asset_id(), project_id=p.id, asset_type="character", name="猎人", version=2,
        source_job_id=None, file_path=None, thumb_path=None, validity="stale",
    )
    session.add_all([a_old, a_new, a_none])
    session.commit()
    yield session, p.id, (a_new.id, a_old.id, a_none.id)
    session.close()


def test_list_assets_ordering_and_fields(db_assets):
    """按关联 Job.created_at 倒序；无关联任务的资产排最后；generated_at 对应任务创建时间。"""
    session, pid, (new_id, old_id, none_id) = db_assets
    r = execute_tool(session, "list_assets", project_id=pid)
    assert r.ok, r.error_message
    ids = [i["asset_id"] for i in r.data["items"]]
    assert ids == [new_id, old_id, none_id]
    items = {i["asset_id"]: i for i in r.data["items"]}
    assert items[none_id]["generated_at"] is None
    assert items[new_id]["generated_at"] is not None
    assert items[new_id]["generated_at"] > items[old_id]["generated_at"]
    assert r.data["count"] == 3
    assert r.data["offset"] == 0


def test_list_assets_hides_filesystem_paths(db_assets):
    """返回项只带 has_file/has_thumb 标识，不暴露服务器目录结构。"""
    session, pid, (new_id, old_id, _none_id) = db_assets
    r = execute_tool(session, "list_assets", project_id=pid)
    items = {i["asset_id"]: i for i in r.data["items"]}
    for item in items.values():
        assert "file_path" not in item
        assert "thumb_path" not in item
        assert "input_snapshot" not in item
    assert items[new_id]["has_file"] is True
    assert items[new_id]["has_thumb"] is False
    assert items[old_id]["has_thumb"] is True


def test_list_assets_pagination(db_assets):
    """limit 与 offset 生效，offset 原样回传。"""
    session, pid, (new_id, old_id, none_id) = db_assets
    r = execute_tool(session, "list_assets", project_id=pid, limit=2, offset=0)
    assert [i["asset_id"] for i in r.data["items"]] == [new_id, old_id]
    assert r.data["count"] == 2 and r.data["offset"] == 0
    r2 = execute_tool(session, "list_assets", project_id=pid, limit=2, offset=2)
    assert [i["asset_id"] for i in r2.data["items"]] == [none_id]
    assert r2.data["count"] == 1 and r2.data["offset"] == 2
    # 越界 offset 返回空清单，不报错
    r3 = execute_tool(session, "list_assets", project_id=pid, offset=99)
    assert r3.ok and r3.data["count"] == 0


def test_list_assets_filters(db_assets):
    """asset_type 与 validity 过滤。"""
    session, pid, (new_id, old_id, none_id) = db_assets
    r = execute_tool(session, "list_assets", project_id=pid, asset_type="character")
    assert {i["asset_id"] for i in r.data["items"]} == {old_id, none_id}
    assert r.data["count"] == 2
    r = execute_tool(session, "list_assets", project_id=pid, validity="current")
    assert r.data["count"] == 2
    assert new_id in {i["asset_id"] for i in r.data["items"]}
    r = execute_tool(session, "list_assets", project_id=pid, asset_type="clip")
    assert r.ok and r.data["count"] == 0


def test_list_assets_arg_bounds(db_assets):
    """limit 超出 1-30、offset 为负、项目不存在分别返回对应错误码。"""
    session, pid, _ids = db_assets
    for bad in ({"limit": 31}, {"limit": 0}, {"offset": -1}):
        r = execute_tool(session, "list_assets", project_id=pid, **bad)
        assert not r.ok
        assert r.error_code == "tool_invalid_args"
    r = execute_tool(session, "list_assets", project_id="prj_notexist")
    assert not r.ok
    assert r.error_code == "project_not_found"


# --- 风格配置工具（读 / 申请 / 审批后执行） ---
@pytest.fixture()
def db_style(db):
    """在通用夹具基础上补一个导演台会话，供审批记录归属。"""
    from datetime import datetime
    from app.models import AgentSession
    from app.models.ids import agent_session_id

    session, pid = db
    sid = agent_session_id()
    session.add(
        AgentSession(
            id=sid,
            title="风格配置测试会话",
            project_id=pid,
            status="active",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
    )
    session.commit()
    return session, pid, sid


def test_get_style_config_returns_normalized_config(db_style):
    """只读工具返回规范化配置、版本与可选项，且不带写副作用。"""
    session, pid, _sid = db_style
    r = execute_tool(session, "get_style_config", project_id=pid)
    assert r.ok, r.error_message
    assert r.data["project_id"] == pid
    assert r.data["version"]
    assert set(r.data["config"]) >= {"visual_style", "genre_tags", "pacing", "target_audience"}
    assert r.data["issues"] == []
    assert r.data["options"]["visual_styles"]


def test_get_style_config_project_not_found(db_style):
    session, _pid, _sid = db_style
    r = execute_tool(session, "get_style_config", project_id="prj_notexist")
    assert not r.ok
    assert r.error_code == "project_not_found"


def test_request_style_update_creates_pending_without_touching_config(db_style):
    """申请阶段只建待审记录，项目配置必须保持原样。"""
    session, pid, sid = db_style
    before = execute_tool(session, "get_style_config", project_id=pid).data["config"]
    r = execute_tool(
        session,
        "request_style_update",
        session_id=sid,
        project_id=pid,
        visual_style="chinese_ink",
    )
    assert r.ok, r.error_message
    assert r.data["status"] == "pending"
    assert r.data["pending_changes"] == {"visual_style": "chinese_ink"}
    after = execute_tool(session, "get_style_config", project_id=pid).data["config"]
    assert after == before


def test_request_style_update_requires_at_least_one_field(db_style):
    session, pid, sid = db_style
    r = execute_tool(session, "request_style_update", session_id=sid, project_id=pid)
    assert not r.ok
    assert r.error_code == "tool_invalid_args"


def test_run_style_update_blocked_without_approval(db_style):
    """未审批时执行工具必须被门禁拦截，不得改配置。"""
    session, pid, sid = db_style
    req = execute_tool(
        session, "request_style_update", session_id=sid, project_id=pid, pacing="tight"
    )
    cid = req.data["confirmation_id"]
    before = execute_tool(session, "get_style_config", project_id=pid).data["config"]
    r = execute_tool(session, "run_style_update", confirmation_id=cid)
    assert not r.ok
    assert r.error_code == "approval_required"
    after = execute_tool(session, "get_style_config", project_id=pid).data["config"]
    assert after == before


def test_run_style_update_blocked_by_execute_tool_gate(db_style):
    """requires_approval 工具未经 approved=True 调用时直接返回门禁码。"""
    session, pid, sid = db_style
    req = execute_tool(
        session, "request_style_update", session_id=sid, project_id=pid, pacing="fast"
    )
    r = execute_tool(session, "run_style_update", confirmation_id=req.data["confirmation_id"])
    assert r.error_code in ("approval_required", "approval_required")


def test_run_style_update_applies_after_approval(db_style):
    """审批通过后执行：配置生效、返回变更字段、版本写入。"""
    from app.services import approvals

    session, pid, sid = db_style
    req = execute_tool(
        session,
        "request_style_update",
        session_id=sid,
        project_id=pid,
        visual_style="chinese_ink",
        pacing="gentle",
        emotional_tone=["温馨"],
    )
    cid = req.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=cid, approve=True)
    r = execute_tool(session, "run_style_update", confirmation_id=cid, approved=True)
    assert r.ok, r.error_message
    assert r.data["config"]["visual_style"] == "chinese_ink"
    assert r.data["config"]["pacing"] == "gentle"
    assert r.data["config"]["emotional_tone"] == ["温馨"]
    assert set(r.data["changed_fields"]) >= {"visual_style", "pacing", "emotional_tone"}
    assert r.data["version"]


def test_run_style_update_normalizes_invalid_value(db_style):
    """审批后执行时非法值被规范化，并返回问题码而非写入脏值。"""
    from app.services import approvals, style_config

    session, pid, sid = db_style
    req = execute_tool(
        session, "request_style_update", session_id=sid, project_id=pid, visual_style="不存在的风格"
    )
    cid = req.data["confirmation_id"]
    approvals.resolve_confirmation(session, confirmation_id=cid, approve=True)
    r = execute_tool(session, "run_style_update", confirmation_id=cid, approved=True)
    assert r.ok, r.error_message
    assert r.data["config"]["visual_style"] == style_config.DEFAULT_VISUAL_STYLE
    assert "visual_style_unknown" in {i["code"] for i in r.data["issues"]}


def test_style_tools_are_routable_by_intent():
    """风格只读工具进入 query/analysis 白名单；写工具不得进入任何只读白名单。"""
    from app.services.agent_tools import get_allowed_tools

    for intent in ("query", "analysis"):
        allowed = get_allowed_tools(intent)
        assert "get_style_config" in allowed
        assert "request_style_update" not in allowed
        assert "run_style_update" not in allowed


def test_style_write_tools_require_approval_chain():
    """写工具的权限、副作用与审批要求必须符合注册表约束。"""
    write_tool = TOOL_REGISTRY["request_style_update"]
    run_tool = TOOL_REGISTRY["run_style_update"]
    read_tool = TOOL_REGISTRY["get_style_config"]
    # 只读工具：无副作用、幂等、不需要审批
    assert read_tool["permission"] == "read"
    assert read_tool["side_effect"] is False
    assert read_tool["requires_approval"] is False
    # 申请工具：写权限但不改状态，本身不须审批（避免死锁）
    assert write_tool["permission"] == "write"
    assert write_tool["side_effect"] is False
    assert write_tool["requires_approval"] is False
    # 执行工具：写权限、有副作用、必须审批
    assert run_tool["permission"] == "write"
    assert run_tool["side_effect"] is True
    assert run_tool["requires_approval"] is True


def test_run_style_update_confirmation_not_found(db_style):
    session, _pid, _sid = db_style
    r = execute_tool(session, "run_style_update", confirmation_id="conf_notexist", approved=True)
    assert not r.ok
    assert r.error_code == "confirmation_not_found"
