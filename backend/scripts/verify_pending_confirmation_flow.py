"""真实多轮对话验证（不使用 LLM）：剧本审核驳回链路端到端。

验证点：
1. 剧本草稿生成后创建带版本号的待确认事项；
2. 用户回复『不可以』→ 关联当前事项、状态 rejected、业务状态写回；
3. 回复不重复询问同一个『是否可以』；
4. 拒绝后资产生成等后续任务被后端门禁阻断；
5. 刷新（重新查询）后拒绝状态与卡片仍可读取。
"""
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import AgentSession, Base, Project, ids
from app.services import pipeline_state as ps
from app.services import review_confirmation as rc
from app.services import video_workflow as vw


def main() -> int:
    engine = make_engine(f"sqlite:////tmp/verify_pending_{int(datetime.now().timestamp())}.db")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db_mod.engine = engine
    db_mod.SessionLocal = factory

    client = TestClient(app)
    now = datetime.now()
    db = factory()
    project = Project(
        id=ids.project_id(),
        name="多轮验证项目",
        aspect_ratio="9:16",
        creation_type="novel",
        status="draft",
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    db.commit()
    session = AgentSession(
        id=ids.agent_session_id(),
        title="多轮验证",
        project_id=project.id,
        status="active",
    )
    db.add(session)
    db.commit()
    # 模拟视频工作流已生成剧本草稿
    vw.set_workflow_state(
        db,
        session,
        {
            "stage": vw.STAGE_SCRIPT_DRAFT,
            "project_id": project.id,
            "script_draft": "第一幕：少年入城。" * 20,
        },
    )
    card = rc.to_card(
        rc.create_script_review(
            db,
            session_id=session.id,
            project_id=project.id,
            question="请确认这份剧本是否可以？",
        )
    )
    sid, pid = session.id, project.id
    db.close()

    print(f"[1] 待审核事项创建: version={card['target_version']} status={card['status']}")

    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "不可以。"})
    assert r.status_code == 200, r.text
    msg = r.json()["assistant_message"]
    print("[2] 用户『不可以』→ 回复:", msg["content"].splitlines()[0])
    payload = msg["payload"]
    print(
        f"[3] 解析结果: resolution={payload.get('resolution')} "
        f"confirmation_status={payload.get('confirmation', {}).get('status')}"
    )
    assert payload.get("resolution") == "reject"
    assert payload["confirmation"]["status"] == "rejected"
    assert "请确认这份剧本是否可以" not in msg["content"], "重复询问同一个确认问题"

    db = factory()
    state = rc.script_review_state(db.get(Project, pid))
    print(f"[4] 业务状态写回: {state}")
    assert state.get("status") == "rejected"
    # 后续依赖任务阻断
    blocked = []
    for action in (
        "analyze_script",
        "generate_character",
        "generate_scene",
        "generate_keyframe",
        "generate_clip",
        "compose_video",
    ):
        try:
            ps.assert_action_allowed(db, pid, action)
        except ps.PipelineStateError:
            blocked.append(action)
    print(f"[5] 被门禁阻断的动作: {blocked}")
    assert len(blocked) == 6
    db.close()

    # 刷新：重新查询确认卡片
    r = client.get(f"/api/agent/sessions/{sid}/reviews")
    items = r.json()["items"]
    print(f"[6] 刷新后卡片: status={items[0]['status']} version={items[0]['target_version']}")
    assert items[0]["status"] == "rejected"

    # 重复提交同一拒绝：不重复写审计消息、不重复改状态
    db = factory()
    before = 0
    from app.models import AgentMessage

    before = (
        db.query(AgentMessage)
        .filter(AgentMessage.session_id == sid, AgentMessage.role == "approval")
        .count()
    )
    db.close()
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "不可以。"})
    assert r.status_code == 200, r.text
    db = factory()
    after = (
        db.query(AgentMessage)
        .filter(AgentMessage.session_id == sid, AgentMessage.role == "approval")
        .count()
    )
    db.close()
    print(f"[7] 重复提交幂等: approval 消息 {before} -> {after}")
    assert before == after

    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
