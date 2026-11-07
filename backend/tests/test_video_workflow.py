"""视频生成工作流状态机测试：剧本确认循环与项目查重分支。

覆盖两处缺陷的回归：
1. 确认循环中带故事名的驳回消息（如『小马过河的剧本不可以』）必须进入
   revise_script 重写分支，不能被项目查重模板（project_exists）劫持；
2. detect_script_confirmation 必须把『不可以/不行』判为 revise，
   『可以/可以了』判为 ok，避免驳回被兜底追问吞掉。
"""
import pytest

from app.core.database import make_engine
from app.models import Base
from app.models.agent import AgentSession


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _make_session(db, stage: str, project_id: str | None = None, **extra) -> AgentSession:
    from app.services import video_workflow as vw

    state = {"stage": stage, "revision_count": 0}
    state.update(extra)
    s = AgentSession(id=f"ags_test_{stage}_{len(state)}", title="工作流测试", status="active")
    s.project_id = project_id
    s.workflow_state = state
    db.add(s)
    db.commit()
    return s


# --- 缺陷 2：确认语义识别 ---

@pytest.mark.parametrize(
    "text,expected",
    [
        ("不可以", "revise"),
        ("不行", "revise"),
        ("这个剧本不可以，重新写", "revise"),
        ("不行，小马应该先问妈妈", "revise"),
        ("太长了吧，精简一下", "revise"),
        ("可以", "ok"),
        ("可以了，保存吧", "ok"),
        ("没问题", "ok"),
    ],
)
def test_detect_script_confirmation(text: str, expected: str):
    from app.services.video_workflow import detect_script_confirmation

    assert detect_script_confirmation(text) == expected


# --- 缺陷 3：故事名提取不应把分集编号当故事名 ---

@pytest.mark.parametrize(
    "text,expected",
    [
        ("生成第一集剧本", None),
        ("生成第2集视频", None),
        ("制作第三期的剧本", None),
        ("生成一个小马过河的视频", "小马过河"),
        ("帮我做龟兔赛跑的剧本", "龟兔赛跑"),
    ],
)
def test_extract_story_name(text: str, expected: str | None):
    from app.services.video_workflow import extract_story_name

    assert extract_story_name(text) == expected


# --- 缺陷 1：确认循环不被查重短路 ---

def test_rejection_with_story_name_revises_instead_of_dedup(db, monkeypatch):
    """script_draft 阶段含故事名的驳回消息应重写剧本，而非返回项目已存在。"""
    from app.api.v1.agent import _handle_video_workflow
    from app.services import video_workflow as vw
    from app.services.pipeline_tools import tool_create_project

    created = tool_create_project(
        db,
        name="小马过河",
        aspect_ratio="16:9",
        creation_type="novel",
        description="剧本风格：童话寓言",
    )
    pid = created["project_id"]

    s = _make_session(
        db,
        vw.STAGE_SCRIPT_DRAFT,
        project_id=pid,
        project_name="小马过河",
        style="童话寓言",
        orientation="16:9",
        script_draft="旧剧本草稿",
    )

    monkeypatch.setattr(vw, "revise_script", lambda *a, **k: "修改后的新剧本")

    reply, payload = _handle_video_workflow(db, s, "小马过河的剧本不可以，改成小马先问妈妈")

    assert payload is not None
    assert payload["workflow"] == "script_revised", payload
    assert "修改后的新剧本" in reply
    db.refresh(s)
    assert s.workflow_state["revision_count"] == 1
    assert s.workflow_state["stage"] == vw.STAGE_SCRIPT_DRAFT


def test_idle_stage_still_dedups_existing_project(db):
    """idle 阶段收到含故事名的生成请求，项目已存在时仍返回查重关联结果。"""
    from app.api.v1.agent import _handle_video_workflow
    from app.services import video_workflow as vw
    from app.services.pipeline_tools import tool_create_project

    created = tool_create_project(
        db,
        name="龟兔赛跑",
        aspect_ratio="16:9",
        creation_type="novel",
        description="剧本风格：童话寓言",
    )
    pid = created["project_id"]

    s = _make_session(db, vw.STAGE_IDLE)

    reply, payload = _handle_video_workflow(db, s, "生成一个龟兔赛跑的视频")

    assert payload["workflow"] == "project_exists", payload
    assert payload["project_id"] == pid
    db.refresh(s)
    assert s.project_id == pid
