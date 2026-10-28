"""流程状态机与流程工具测试（导演台 Agent 执行流程 P2）。

覆盖：
- 状态机节点判定（12 阶段 → 流程节点映射）
- 操作门禁（操作不得早于其所属节点，阻断抛 PipelineStateError）
- 流程工具执行（保存剧本/分析剧本/流程状态查询）
- 审批纪律（合成/导出未批准不执行，返回 approval_required 且无副作用）
- 工具元数据完整性（权限/风险/审批/入参 Schema）
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Job, Project
from app.models.ids import project_id


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    session = factory()
    p = Project(
        id=project_id(), name="流程测试项目", aspect_ratio="16:9",
        creation_type="script", status="active",
        created_at=datetime.now(), updated_at=datetime.now(),
    )
    session.add(p)
    session.commit()
    yield session, p.id
    session.close()


def test_pipeline_nodes_ordered():
    """流程节点链有序且覆盖完整制作流程，审核闸口节点正确标记。"""
    from app.services.pipeline_state import PIPELINE_NODES
    keys = [n.key for n in PIPELINE_NODES]
    assert keys == [
        "create_project", "save_script", "analyze_script", "generate_assets",
        "review_assets", "generate_keyframes", "review_keyframes",
        "generate_clips", "review_clips", "compose",
    ]
    gates = [n.key for n in PIPELINE_NODES if n.review_gate]
    assert gates == ["review_assets", "review_keyframes", "review_clips"]


def test_get_pipeline_status_empty_project(db):
    """空项目（无剧本）当前阶段为 estimate，节点落在新建项目/保存剧本域。"""
    from app.services.pipeline_state import get_pipeline_status
    session, pid = db
    st = get_pipeline_status(session, pid)
    assert st.stage == "estimate"
    assert st.node_key in ("create_project", "save_script")
    assert st.next_hint


def test_gate_blocks_early_actions(db):
    """状态机门禁：空项目不能审核资产/确认分镜/合成/导出。"""
    from app.services.pipeline_state import PipelineStateError, assert_action_allowed
    session, pid = db
    for action in ("review_assets", "confirm_keyframe", "compose_video", "export_video"):
        with pytest.raises(PipelineStateError) as exc:
            assert_action_allowed(session, pid, action)
        assert exc.value.required_node


def test_save_and_analyze_script_flow(db):
    """保存剧本 → 分析剧本：流程工具执行并返回流程状态与下一步指引。"""
    from app.services.pipeline_tools import execute_pipeline_tool
    session, pid = db
    r = execute_pipeline_tool(
        session, "save_script", project_id=pid,
        text="第一幕：少年离家。\n第二幕：江湖历练。",
    )
    assert r.ok, r.error_message
    assert r.data["version"] == 1
    assert r.data["pipeline_node"]
    assert r.data["next_hint"]
    r2 = execute_pipeline_tool(session, "analyze_script", project_id=pid)
    assert r2.ok, r2.error_message
    assert r2.data["job_id"].startswith("job_")
    assert r2.data["job_status"] == "queued"


def test_review_assets_requires_assets(db):
    """资产审核：无资产时 approve 被资产门禁阻断（不跳过审核闸口）。"""
    from app.services.pipeline_tools import execute_pipeline_tool
    session, pid = db
    execute_pipeline_tool(session, "save_script", project_id=pid, text="第一幕：少年离家。")
    r = execute_pipeline_tool(session, "review_assets", project_id=pid, action="approve")
    assert not r.ok


def test_compose_requires_approval(db):
    """合成工具需人工审批：未批准时返回 approval_required 且不产生合成任务。"""
    from app.services.pipeline_tools import execute_pipeline_tool
    session, pid = db
    r = execute_pipeline_tool(session, "compose_video", project_id=pid)
    assert not r.ok
    assert r.error_code == "approval_required"
    assert (
        session.query(Job)
        .filter(Job.project_id == pid, Job.job_type == "compose")
        .count()
        == 0
    )


def test_export_requires_approval(db):
    """导出工具需人工审批：未批准时返回 approval_required。"""
    from app.services.pipeline_tools import execute_pipeline_tool
    session, pid = db
    r = execute_pipeline_tool(
        session, "export_video", project_id=pid, composition_id="cmp_nonexist"
    )
    assert not r.ok
    assert r.error_code == "approval_required"


def test_get_pipeline_status_tool(db):
    """流程状态查询工具：返回节点、阶段与下一步指引。"""
    from app.services.pipeline_tools import execute_pipeline_tool
    session, pid = db
    r = execute_pipeline_tool(session, "get_pipeline_status", project_id=pid)
    assert r.ok
    assert r.data["pipeline_node"]
    assert r.data["next_hint"]


def test_pipeline_tool_specs_metadata():
    """流程工具元数据完整：权限/风险/审批/入参 Schema 齐备。"""
    from app.services.pipeline_tools import pipeline_tool_specs
    specs = {s["name"]: s for s in pipeline_tool_specs()}
    assert set(specs) == {
        "create_project", "save_script", "analyze_script", "review_assets",
        "confirm_keyframes", "compose_video", "export_video", "get_pipeline_status",
    }
    for _name, s in specs.items():
        assert s["permission"] in {"read", "write", "generate"}
        assert s["risk_level"] in {"low", "medium", "high"}
        assert s["input_schema"] is not None
    assert specs["compose_video"]["requires_approval"] is True
    assert specs["export_video"]["requires_approval"] is True
    assert specs["get_pipeline_status"]["side_effect"] is False
