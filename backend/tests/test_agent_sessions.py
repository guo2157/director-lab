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

    r = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"role": "assistant", "content": "回复", "payload": {"tool": "none"}},
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

    def fake_chat_messages(self, messages, *, json_mode=False):
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

    def raise_unconfigured(self, messages, *, json_mode=False):
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

    def raise_timeout(self, messages, *, json_mode=False):
        raise LLMError("llm_timeout", "语言模型请求超时")

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", raise_timeout)
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "hi again"})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "llm_timeout"
