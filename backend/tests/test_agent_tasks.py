"""Agent 任务执行体系测试（Phase 2）。

覆盖：
- 工具前置条件阻断（agent_tools.execute_tool preconditions）
- DAG 调度器（schedule_ready_stages / 失败级联 / 取消传播 / 并发控制）
- 结果聚合器（aggregate_task_results 统计与幂等）
- 聚合校验（verify_aggregate_completeness / verify_stage_dependencies）
"""
from datetime import datetime
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import (
    Asset,
    Base,
    ParseVersion,
    Project,
    ScriptVersion,
    ids,
)
from app.models.task import TaskResult, TaskStage
from app.services import task_orchestrator as to
from app.services.agent_tools import (
    _precheck_assets_exist,
    _precheck_project_exists,
    _precheck_script_exists,
    _precheck_script_parsed,
    execute_tool,
)
from app.services.execution_verifier import (
    verify_aggregate_completeness,
    verify_stage_dependencies,
)
from app.services.parent_agent import aggregate_task_results


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'agent_task.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _mk_project(
    db,
    *,
    reviewed: bool = False,
    with_script: bool = False,
    with_parse: bool = False,
    with_asset: bool = False,
) -> Project:
    """构造项目（可带剧本/解析/资产），满足各前置条件组合。"""
    now = datetime.now()
    project = Project(
        id=ids.project_id(),
        name="测试项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
    )
    if reviewed:
        project.assets_review_status = "approved"
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
            status="completed",
            created_at=now,
        )
        db.add(pv)
        db.flush()
        project.current_parse_version_id = pv.id
    if with_asset:
        asset = Asset(
            id=ids.asset_id(),
            project_id=project.id,
            asset_type="character",
            name="角色图",
            version=1,
            validity="current",
            file_path="placeholder.png",
        )
        db.add(asset)
    db.commit()
    return project


# ---------- 前置条件 ----------


def test_precheck_project_exists(db):
    ok, msg = _precheck_project_exists(db, project_id="missing_prj")
    assert not ok and "项目不存在" in msg
    p = _mk_project(db)
    ok, _ = _precheck_project_exists(db, project_id=p.id)
    assert ok


def test_precheck_script_exists(db):
    p = _mk_project(db)
    ok, _ = _precheck_script_exists(db, project_id=p.id)
    assert not ok
    p2 = _mk_project(db, with_script=True)
    ok, _ = _precheck_script_exists(db, project_id=p2.id)
    assert ok


def test_precheck_script_parsed(db):
    p = _mk_project(db, with_script=True)
    ok, _ = _precheck_script_parsed(db, project_id=p.id)
    assert not ok
    p2 = _mk_project(db, with_script=True, with_parse=True)
    ok, _ = _precheck_script_parsed(db, project_id=p2.id)
    assert ok


def test_precheck_assets_exist(db):
    p = _mk_project(db)
    ok, _ = _precheck_assets_exist(db, project_id=p.id)
    assert not ok
    p2 = _mk_project(db, with_asset=True)
    ok, _ = _precheck_assets_exist(db, project_id=p2.id)
    assert ok


def test_execute_tool_precondition_blocks(db):
    """前置条件不满足时 execute_tool 返回 precondition_failed，不执行 fn。"""
    p = _mk_project(db)  # 无剧本
    r = execute_tool(db, "script_normalize", project_id=p.id)
    assert not r.ok
    assert r.error_code == "precondition_failed"


def test_execute_tool_precondition_passes(db):
    """前置条件满足后进入执行（此处验证不再因前置条件被阻断）。"""
    p = _mk_project(db, with_script=True)
    r = execute_tool(db, "script_normalize", project_id=p.id)
    # 允许内部业务错误，但必须已通过前置条件（不再返回 precondition_failed）
    assert r.error_code != "precondition_failed"


# ---------- DAG 调度器 ----------


def test_schedule_ready_stages_sequential(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="DAG")
    to.start_task(db, task)
    s1 = to.add_stage(db, task, stage_key="a", stage_name="A")
    s2 = to.add_stage(db, task, stage_key="b", stage_name="B", depends_on=["a"])
    started = to.schedule_ready_stages(db, task)
    assert started == 1
    assert s1.status == "running"
    assert s2.status == "pending"
    # finish_stage 内部自动调度下一批就绪阶段
    to.finish_stage(db, task, s1, "succeeded")
    db.flush()
    assert s2.status == "running"


def test_failure_cascade_skips_dependents(db):
    task = to.create_task(db, session_id="s1", task_type="pipeline", title="级联")
    s1 = to.add_stage(db, task, stage_key="a", stage_name="A")
    s2 = to.add_stage(db, task, stage_key="b", stage_name="B", depends_on=["a"])
    to.start_stage(db, task, s1)
    to.finish_stage(db, task, s1, "failed")
    db.flush()
    assert s2.status == "skipped"
    assert s2.result and s2.result.get("blocked_by") == "a"


def test_cancel_task_propagates_to_stages(db):
    task = to.create_task(db, session_id="s1", task_type="generate", title="取消传播")
    s1 = to.add_stage(db, task, stage_key="a", stage_name="A")
    s2 = to.add_stage(db, task, stage_key="b", stage_name="B")
    to.start_stage(db, task, s1)
    assert to.cancel_task(db, task, reason="user_cancel") is True
    assert task.status == "cancelled"
    assert s1.status == "cancelled"
    assert s2.status == "cancelled"


def test_max_concurrent_stages(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="并发")
    to.start_task(db, task)
    stages = [
        to.add_stage(db, task, stage_key=f"s{i}", stage_name=f"S{i}")
        for i in range(10)
    ]
    started = to.schedule_ready_stages(db, task)
    assert started == 8
    assert sum(1 for s in stages if s.status == "running") == 8


# ---------- 结果聚合器 ----------


def test_aggregate_task_results_stats_and_idempotent(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="聚合")
    s1 = to.add_stage(db, task, stage_key="a", stage_name="A")
    s2 = to.add_stage(db, task, stage_key="b", stage_name="B")
    to.start_stage(db, task, s1)
    to.finish_stage(db, task, s1, "succeeded")
    to.start_stage(db, task, s2)
    to.finish_stage(db, task, s2, "failed", result={"error_msg": "解析失败"})

    agg = aggregate_task_results(db, task_id=task.id)
    assert agg["total"] == 2
    assert agg["items"] == 1
    assert agg["errors"] == 1
    assert agg["error_items"][0]["stage_key"] == "b"
    assert not agg["incomplete"]

    # 幂等：再次聚合不新增卡片，仅更新
    aggregate_task_results(db, task_id=task.id)
    cards = (
        db.query(TaskResult)
        .filter(TaskResult.task_id == task.id, TaskResult.result_type == "aggregate")
        .all()
    )
    assert len(cards) == 1
    assert cards[0].data["total"] == 2


def test_aggregate_task_results_incomplete(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="未完成")
    to.add_stage(db, task, stage_key="a", stage_name="A")
    to.add_stage(db, task, stage_key="b", stage_name="B", depends_on=["a"])
    agg = aggregate_task_results(db, task_id=task.id)
    assert agg["total"] == 2
    assert agg["incomplete"] is True


def test_aggregate_task_results_not_found(db):
    agg = aggregate_task_results(db, task_id="task_missing_1")
    assert agg["error"] == "task_not_found"


# ---------- 聚合校验 ----------


def test_verify_aggregate_completeness_ok(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="校验")
    s1 = to.add_stage(db, task, stage_key="a", stage_name="A")
    to.start_stage(db, task, s1)
    to.finish_stage(db, task, s1, "succeeded")
    aggregate_task_results(db, task_id=task.id)
    v = verify_aggregate_completeness(db, task_id=task.id)
    assert v.verified


def test_verify_aggregate_completeness_missing(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="缺卡片")
    v = verify_aggregate_completeness(db, task_id=task.id)
    assert not v.verified
    assert v.reason_code == "aggregate_missing"


def test_verify_aggregate_completeness_status_mismatch(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="状态不一致")
    s1 = to.add_stage(db, task, stage_key="a", stage_name="A")
    to.start_stage(db, task, s1)
    to.finish_stage(db, task, s1, "succeeded")
    to.add_stage(db, task, stage_key="b", stage_name="B", depends_on=["a"])
    aggregate_task_results(db, task_id=task.id)
    # 篡改聚合统计：模拟数量守恒通过但状态计数错误
    cards = (
        db.query(TaskResult)
        .filter(TaskResult.task_id == task.id, TaskResult.result_type == "aggregate")
        .all()
    )
    cards[0].data["items"] = 99
    db.flush()
    v = verify_aggregate_completeness(db, task_id=task.id)
    assert not v.verified
    assert v.reason_code == "aggregate_status_mismatch"


# ---------- DAG 依赖校验 ----------


def test_verify_stage_dependencies_ok(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="依赖合法")
    to.add_stage(db, task, stage_key="a", stage_name="A")
    to.add_stage(db, task, stage_key="b", stage_name="B", depends_on=["a"])
    v = verify_stage_dependencies(db, task_id=task.id)
    assert v.verified


def test_verify_stage_dependencies_missing(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="依赖缺失")
    to.add_stage(db, task, stage_key="b", stage_name="B", depends_on=["ghost"])
    v = verify_stage_dependencies(db, task_id=task.id)
    assert not v.verified
    assert v.reason_code == "dependency_missing"


def test_verify_stage_dependencies_inversion(db):
    task = to.create_task(db, session_id="s1", task_type="parse", title="依赖倒置")
    to.add_stage(db, task, stage_key="a", stage_name="A")
    to.add_stage(db, task, stage_key="b", stage_name="B")
    # 手动构造倒置：b（后置）被 a（前置）依赖
    stages = (
        db.query(TaskStage)
        .filter(TaskStage.task_id == task.id)
        .order_by(TaskStage.stage_index)
        .all()
    )
    stages[0].depends_on = ["b"]
    db.flush()
    v = verify_stage_dependencies(db, task_id=task.id)
    assert not v.verified
    assert v.reason_code == "dependency_inversion"
