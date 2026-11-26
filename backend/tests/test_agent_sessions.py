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


def test_chat_attachments_upload_and_reference(client: TestClient, monkeypatch, tmp_path):
    """多图参考：上传 → 读取 → 挂到用户消息 → 注入 LLM 上下文；非法输入拒绝。"""
    import app.api.v1.agent as agent_mod
    from types import SimpleNamespace

    # 附件落盘到临时目录，避免污染真实 media_dir
    monkeypatch.setattr(
        agent_mod, "get_settings", lambda: SimpleNamespace(media_dir=str(tmp_path / "media"))
    )
    captured: dict = {}

    def fake_chat_messages(self, messages, *, json_mode=False, max_tokens=None):
        captured["messages"] = messages
        return "收到参考图"

    monkeypatch.setattr(agent_mod.LLMClient, "chat_messages", fake_chat_messages)

    sid = client.post("/api/agent/sessions", json={"title": "多图参考"}).json()["id"]
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    r = client.post(
        f"/api/agent/sessions/{sid}/attachments",
        files=[
            ("files", ("a.png", png, "image/png")),
            ("files", ("b.jpg", png, "image/jpeg")),
        ],
    )
    assert r.status_code == 201, r.text
    items = r.json()["items"]
    assert [it["name"] for it in items] == ["a.png", "b.jpg"]
    assert all(it["url"].startswith(f"/api/agent/sessions/{sid}/attachments/") for it in items)

    # 文件可读取且内容一致
    r = client.get(items[0]["url"])
    assert r.status_code == 200
    assert r.content == png

    # 聊天：附件元数据落到用户消息 payload，并注入本轮 LLM 上下文
    refs = [{"id": it["id"], "name": it["name"]} for it in items]
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": "你好", "attachments": refs})
    assert r.status_code == 200, r.text
    stored = r.json()["user_message"]["payload"]["attachments"]
    assert [a["name"] for a in stored] == ["a.png", "b.jpg"]
    assert all(a["url"] and a["size"] > 0 for a in stored)

    last_user = [m for m in captured["messages"] if m["role"] == "user"][-1]
    assert "a.png" in last_user["content"] and "b.jpg" in last_user["content"]
    assert "不要虚构图片内容" in last_user["content"]

    # 纯图片消息（content 为空）允许发送
    r = client.post(
        f"/api/agent/sessions/{sid}/chat", json={"content": "", "attachments": refs[:1]}
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["user_message"]["payload"]["attachments"]) == 1

    # 非图片扩展名拒绝
    r = client.post(
        f"/api/agent/sessions/{sid}/attachments",
        files=[("files", ("x.txt", b"hi", "text/plain"))],
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_attachment"

    # 伪造附件标识（路径穿越）在 chat 阶段被拒绝
    r = client.post(
        f"/api/agent/sessions/{sid}/chat",
        json={"content": "hi", "attachments": [{"id": "../../etc/passwd", "name": "x"}]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_attachment"
