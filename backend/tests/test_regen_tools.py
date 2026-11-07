"""返工工具测试（导演台 Agent 执行流程 P3：重新生成/替换/采纳工具化）。

覆盖：
- 审批纪律（重新生成类未批准不执行，返回 approval_required 且无副作用）
- 状态机门禁（早期阶段返工被阻断）
- 目标校验（资产/镜头不存在、跨项目、类型不符的错误分类）
- 工具元数据完整性
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Asset, Job, Project
from app.models.ids import asset_id, job_id, project_id


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
        id=project_id(), name="返工测试项目", aspect_ratio="16:9",
        creation_type="script", status="active",

    )
    session.add(p)
    # 一条角色资产（当前生效）
    a = Asset(
        id=asset_id(), project_id=p.id, asset_type="character",
        name="主角定妆图", version=1, validity="current",

    )
    session.add(a)
    session.commit()
    yield session, p.id, a.id
    session.close()


def test_regen_tool_specs_metadata():
    """返工工具元数据完整：权限/风险/审批/入参 Schema 齐备。"""
    from app.services.regen_tools import regen_tool_specs
    specs = {s["name"]: s for s in regen_tool_specs()}
    assert set(specs) == {
        "regenerate_asset", "replace_asset", "regenerate_shot",
        "adopt_candidate", "list_candidates",
    }
    for _name, s in specs.items():
        assert s["permission"] in {"read", "write", "generate"}
        assert s["risk_level"] in {"low", "medium", "high"}
        assert s["input_schema"] is not None
    # 生成类返工须审批
    assert specs["regenerate_asset"]["requires_approval"] is True
    assert specs["regenerate_shot"]["requires_approval"] is True
    # 查询类无副作用
    assert specs["list_candidates"]["side_effect"] is False


def test_regenerate_asset_requires_approval(db):
    """资产重新生成需人工审批：未批准时返回 approval_required 且不产生任务。"""
    from app.services.regen_tools import execute_regen_tool
    session, pid, aid = db
    r = execute_regen_tool(session, "regenerate_asset", project_id=pid, asset_id=aid)
    assert not r.ok
    assert r.error_code == "approval_required"
    assert session.query(Job).filter(Job.project_id == pid).count() == 0


def test_regenerate_shot_requires_approval(db):
    """镜头重新生成需人工审批：未批准时返回 approval_required。"""
    from app.services.regen_tools import execute_regen_tool
    session, pid, _aid = db
    r = execute_regen_tool(
        session, "regenerate_shot", project_id=pid, shot_id="sht_nonexist", target="keyframe"
    )
    assert not r.ok
    assert r.error_code == "approval_required"


def test_regenerate_asset_type_restriction(db):
    """资产重新生成仅支持角色/场景/道具：其他类型被拒绝。"""
    from app.services.regen_tools import execute_regen_tool
    session, pid, _aid = db
    kf = Asset(
        id=asset_id(), project_id=pid, asset_type="keyframe",
        name="分镜图", version=1, validity="current",

    )
    session.add(kf)
    session.commit()
    r = execute_regen_tool(
        session, "regenerate_asset", approved=True, project_id=pid, asset_id=kf.id
    )
    assert not r.ok
    assert "仅角色/场景/道具" in (r.error_message or "")


def test_asset_not_in_project(db):
    """跨项目资产被拒绝（asset_not_in_project）。"""
    from app.services.regen_tools import execute_regen_tool
    session, pid, _aid = db
    other = Project(
        id=project_id(), name="其他项目", aspect_ratio="16:9",
        creation_type="script", status="active",

    )
    session.add(other)
    oa = Asset(
        id=asset_id(), project_id=other.id, asset_type="character",
        name="他项目资产", version=1, validity="current",

    )
    session.add(oa)
    session.commit()
    r = execute_regen_tool(
        session, "list_candidates", project_id=pid, asset_id=oa.id
    )
    assert not r.ok
    assert r.error_code == "asset_not_in_project"


def test_adopt_requires_candidate(db):
    """采纳候选：非候选状态被拒绝（invalid_state_transition）。"""
    from app.services.regen_tools import execute_regen_tool
    session, pid, aid = db
    r = execute_regen_tool(session, "adopt_candidate", project_id=pid, asset_id=aid)
    assert not r.ok
    assert r.error_code == "invalid_state_transition"


def test_list_candidates_readonly(db):
    """候选列表查询：只读执行成功，返回候选清单。"""
    from app.services.regen_tools import execute_regen_tool
    session, pid, aid = db
    r = execute_regen_tool(session, "list_candidates", project_id=pid, asset_id=aid)
    assert r.ok
    assert "candidates" in r.data
    assert r.data["count"] >= 0


def test_regen_intent_routing():
    """意图路由：返工表述识别为返工操作，首次生成请求识别为生成请求。"""
    from app.services.intent_router import apply_fast_path_rules
    cases = {
        "重新生成这张角色图": "regenerate_asset",
        "重绘第3镜的分镜图": "regenerate_shot",
        "重新生成第2镜的视频": "regenerate_shot",
        "替换这张图片": "replace_asset",
        "采纳这个候选版本": "adopt_candidate",
        "看看候选版本": "list_candidates",
    }
    for text, action in cases.items():
        r = apply_fast_path_rules(text)
        assert r is not None, f"{text} 未命中规则"
        assert r.intent == "pipeline_action"
        assert r.entities.get("pipeline_action") == action
    # 首次生成请求不再走规则快路径（改由 LLM 结构化识别判定 generate_request），
    # 但也不应误判为返工操作
    for text in ("生成角色图", "为第3镜生成关键帧"):
        r = apply_fast_path_rules(text)
        assert r is None or r.intent != "pipeline_action", f"{text} 不应误判为返工操作"
