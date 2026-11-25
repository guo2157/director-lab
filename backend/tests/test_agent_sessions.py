"""导演台 Agent 会话与消息接口测试。

覆盖：会话 CRUD（创建/列表/详情/重命名/归档/恢复）、消息追加与 seq 递增、
增量加载（after_seq）、归档会话只读保护、关联项目校验。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
from app.models import Base


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    return TestClient(app)


def test_session_crud_flow(client: TestClient):
    """会话创建 → 列表 → 重命名 → 归档 → 恢复 全链路。"""
    # 创建
    r = client.post("/api/agent/sessions", json={"title": "测试会话"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["status"] == "active"
    assert r.json()["message_count"] == 0

    # 列表可见
    r = client.get("/api/agent/sessions")
    assert r.status_code == 200
    assert any(s["id"] == sid for s in r.json()["items"])

    # 重命名
    r = client.patch(f"/api/agent/sessions/{sid}", json={"title": "改名后"})
    assert r.status_code == 200
    assert r.json()["title"] == "改名后"

    # 归档（软删除）
    r = client.delete(f"/api/agent/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "archived"

    # 默认列表不含归档会话
    r = client.get("/api/agent/sessions")
    assert not any(s["id"] == sid for s in r.json()["items"])

    # 恢复
    r = client.post(f"/api/agent/sessions/{sid}/restore")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_message_append_and_seq(client: TestClient):
    """消息追加：seq 递增、列表按序返回、after_seq 增量加载。"""
    r = client.post("/api/agent/sessions", json={"title": "消息测试"})
    sid = r.json()["id"]

    for i in range(3):
        r = client.post(
            f"/api/agent/sessions/{sid}/messages",
            json={"role": "user", "content": f"消息{i}"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["seq"] == i + 1

    # P0-4 API 输入安全（2026-11-25）：外部追加消息仅允许 user 角色；
    # assistant/system/tool_event/approval 为服务端内部角色，客户端伪造应被 422 拒绝
    r = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"role": "assistant", "content": "回复", "payload": {"tool": "none"}},
    )
    assert r.status_code == 422
    # 再次以 user 角色追加，seq 应继续递增为 4
    r = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"role": "user", "content": "消息3"},
    )
    assert r.status_code == 201
    assert r.json()["seq"] == 4

    # 全量列表按 seq 升序
    r = client.get(f"/api/agent/sessions/{sid}/messages")
    assert r.status_code == 200
    seqs = [m["seq"] for m in r.json()["items"]]
    assert seqs == [1, 2, 3, 4]

    # 增量加载
    r = client.get(f"/api/agent/sessions/{sid}/messages", params={"after_seq": 2})
    seqs = [m["seq"] for m in r.json()["items"]]
    assert seqs == [3, 4]

    # 会话派生字段更新
    r = client.get(f"/api/agent/sessions/{sid}")
    assert r.json()["message_count"] == 4


def test_archived_session_rejects_append(client: TestClient):
    """归档会话只读：追加消息返回 409。"""
    r = client.post("/api/agent/sessions", json={"title": "归档测试"})
    sid = r.json()["id"]
    client.delete(f"/api/agent/sessions/{sid}")
    r = client.post(
        f"/api/agent/sessions/{sid}/messages", json={"role": "user", "content": "x"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "session_archived"


def test_invalid_role_rejected(client: TestClient):
    """非法 role 被 422 拒绝。"""
    r = client.post("/api/agent/sessions", json={"title": "r"})
    sid = r.json()["id"]
    r = client.post(
        f"/api/agent/sessions/{sid}/messages", json={"role": "invalid", "content": "x"}
    )
    assert r.status_code == 422


def test_session_not_found(client: TestClient):
    """不存在的会话返回 404。"""
    r = client.get("/api/agent/sessions/ags_notexist")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "agent_session_not_found"


def test_project_link_validation(client: TestClient):
    """关联不存在的项目返回 404。"""
    r = client.post(
        "/api/agent/sessions", json={"title": "p", "project_id": "prj_notexist"}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "project_not_found"


def test_chat_flow(client: TestClient, monkeypatch):
    """聊天接口：用户消息与助手回复持久化、seq 连续、历史参与上下文。"""
    import app.api.v1.agent as agent_mod
    from app.integrations.llm import LLMError

    captured: dict = {}

    def fake_chat_messages(self, messages, *, json_mode=False, max_tokens=None):
        captured["messages"] = messages
        return "这是助手的回复"

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", fake_chat_messages)

    r = client.post("/api/agent/sessions", json={"title": "聊天"})
    sid = r.json()["id"]

    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "你好"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_message"]["role"] == "user"
    assert body["user_message"]["content"] == "你好"
    assert body["assistant_message"]["role"] == "assistant"
    assert body["assistant_message"]["content"] == "这是助手的回复"
    assert body["assistant_message"]["seq"] == body["user_message"]["seq"] + 1

    # 系统提示词在首位，用户消息参与上下文
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert any(m["role"] == "user" and m["content"] == "你好" for m in msgs)

    # 第二轮：历史（含第一轮助手回复）参与上下文
    client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "继续"})
    msgs = captured["messages"]
    assert any(m["role"] == "assistant" and m["content"] == "这是助手的回复" for m in msgs)

    # 消息列表包含全部 4 条
    r = client.get(f"/api/agent/sessions/{sid}/messages")
    assert len(r.json()["items"]) == 4


def test_chat_llm_error_mapping(client: TestClient, monkeypatch):
    """LLM 错误分类：未配置返回 409，超时/不可达返回 502；用户消息仍已持久化。"""
    import app.api.v1.agent as agent_mod
    from app.integrations.llm import LLMError

    def raise_unconfigured(self, messages, *, json_mode=False, max_tokens=None):
        raise LLMError("llm_unconfigured", "语言模型未配置 API Key")

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", raise_unconfigured)

    r = client.post("/api/agent/sessions", json={"title": "e"})
    sid = r.json()["id"]
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "llm_unconfigured"

    # 用户消息已持久化（失败可恢复，前端可重试）
    r = client.get(f"/api/agent/sessions/{sid}/messages")
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["role"] == "user"

    def raise_timeout(self, messages, *, json_mode=False, max_tokens=None):
        raise LLMError("llm_timeout", "语言模型请求超时")

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", raise_timeout)
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "hi again"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "llm_timeout"


def test_chat_asset_refs_gated_by_keywords(client: TestClient, monkeypatch):
    """资产类消息（含『看看/图』等关键词）注入 list_assets 并回传 asset_refs；
    进展类消息不注入缩略图，避免每条回复都附带图片（UI 噪音与无谓带宽）。
    """
    import app.api.v1.agent as agent_mod
    import app.core.database as db_mod
    from datetime import datetime
    from app.models import Asset, Job
    from app.models.ids import asset_id, job_id

    monkeypatch.setattr(
        agent_mod.LLMClient,
        "chat_messages",
        lambda self, messages, *, json_mode=False: "这是助手的回复",
    )
    r = client.post("/api/projects", json={"name": "气泡缩略图", "aspect_ratio": "9:16"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    db = db_mod.SessionLocal()
    now = datetime.now()
    job = Job(id=job_id(), project_id=pid, job_type="image", status="succeeded", created_at=now)
    db.add(job)
    asset = Asset(
        id=asset_id(), project_id=pid, asset_type="character", name="小红帽", version=1,
        source_job_id=job.id, file_path="/tmp/a.png", thumb_path="/tmp/a.webp", validity="current",
    )
    db.add(asset)
    db.commit()
    aid = asset.id
    db.close()

    r = client.post("/api/agent/sessions", json={"title": "缩略图", "project_id": pid})
    sid = r.json()["id"]

    # 1) 资产类消息：命中关键词 → 执行 list_assets → payload.asset_refs 供前端渲染缩略图
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "看看角色图长什么样"})
    assert r.status_code == 200, r.text
    refs = r.json()["assistant_message"]["payload"]["asset_refs"]
    assert [x["asset_id"] for x in refs] == [aid]
    assert refs[0]["asset_type"] == "character"
    assert refs[0]["has_thumb"] is True
    # 只暴露元数据，不泄露服务器目录结构
    assert "file_path" not in refs[0]
    assert "thumb_path" not in refs[0]
    # 审计：list_assets 的 tool_event 已落库
    msgs = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
    tool_names = [m["payload"]["tool_name"] for m in msgs if m["role"] == "tool_event"]
    assert "list_assets" in tool_names

    # 2) 进展类消息：不含关键词 → 不执行 list_assets，asset_refs 为空数组
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "项目进展如何"})
    assert r.status_code == 200, r.text
    assert r.json()["assistant_message"]["payload"]["asset_refs"] == []
    msgs = client.get(f"/api/agent/sessions/{sid}/messages").json()["items"]
    tool_names = [m["payload"]["tool_name"] for m in msgs if m["role"] == "tool_event"]
    assert tool_names.count("list_assets") == 1
    # tool_event 不进入对话上下文（仅 user/assistant 参与）
    roles = {m["role"] for m in msgs}
    assert "tool_event" in roles


def test_project_switch_resets_summary(client: TestClient):
    """P0-1 项目切换摘要隔离（2026-11-25）：切换 project_id 后，旧项目摘要与
    摘要检查点被重置（不注入新项目执行上下文），历史消息完整保留。"""
    from datetime import datetime
    import app.core.database as db_mod
    from app.models import AgentSession, Project
    from app.models import ids

    # 造两个真实项目（关联校验要求项目存在）
    now = datetime.now()
    db = db_mod.SessionLocal()
    try:
        for pid, name in (("prj_switcha00001", "项目A"), ("prj_switchb00001", "项目B")):
            db.add(Project(
                id=pid, name=name, aspect_ratio="9:16", creation_type="script",
                status="active", created_at=now, updated_at=now,
            ))
        db.commit()
    finally:
        db.close()

    # 建会话并关联项目A
    r = client.post("/api/agent/sessions", json={"title": "切换测试", "project_id": "prj_switcha00001"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # 直接落库摘要与检查点（模拟项目A上下文已压缩）
    db = db_mod.SessionLocal()
    try:
        s = db.get(AgentSession, sid)
        s.summary = "项目A摘要：角色小红帽，风格写实"
        s.summary_upto_seq = 5
        db.commit()
    finally:
        db.close()

    # 切换到项目B：摘要与检查点应被重置
    r = client.patch(f"/api/agent/sessions/{sid}", json={"project_id": "prj_switchb00001"})
    assert r.status_code == 200, r.text
    db = db_mod.SessionLocal()
    try:
        s = db.get(AgentSession, sid)
        assert s.project_id == "prj_switchb00001"
        assert s.summary is None, "切换项目后旧项目摘要不得保留"
        assert s.summary_upto_seq is None, "切换项目后摘要检查点不得保留"
    finally:
        db.close()

    # 历史消息完整保留（切换项目不删除对话记录）
    r = client.post(f"/api/agent/sessions/{sid}/messages", json={"role": "user", "content": "历史消息"})
    assert r.status_code == 201
    r = client.get(f"/api/agent/sessions/{sid}/messages")
    assert len(r.json()["items"]) >= 1

    # 取消关联（project_id=null）同样触发重置
    db = db_mod.SessionLocal()
    try:
        s = db.get(AgentSession, sid)
        s.summary = "项目B摘要"
        s.summary_upto_seq = 8
        db.commit()
    finally:
        db.close()
    r = client.patch(f"/api/agent/sessions/{sid}", json={"project_id": None})
    assert r.status_code == 200, r.text
    db = db_mod.SessionLocal()
    try:
        s = db.get(AgentSession, sid)
        assert s.project_id is None
        assert s.summary is None
        assert s.summary_upto_seq is None
    finally:
        db.close()
