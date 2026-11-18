"""会话标题自动生成测试。

覆盖：
- is_default_title 判定（哪些标题算"未命名"）
- _clean_title 清洗（去引号/书名号/结尾标点/前缀噪音/超长截断）
- _fallback_title 本地降级
- generate_session_title 的三个关键约束：只在默认标题时命名、LLM 失败降级、
  已手动命名的绝不覆盖
- auto-title 端点：幂等、重命名后不再改名

LLM 全部用 monkeypatch 替换，不依赖网关。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base
from app.models.agent import AgentMessage, AgentSession
from app.services import session_title as st


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", factory)
    db = factory()
    yield db
    db.close()


@pytest.fixture()
def client(db_session):
    return TestClient(app)


def _make_session(db, title="新会话", sid="ags_test01"):
    s = AgentSession(id=sid, title=title, status="active")
    db.add(s)
    db.commit()
    return s


def _add_turns(db, sid, user_text, assistant_text="好的"):
    """构造首轮 user + assistant 消息。created_at 必须显式给（模型无默认值）。"""
    from datetime import datetime

    now = datetime.now()
    db.add(
        AgentMessage(
            id=f"msg_u_{sid}",
            session_id=sid,
            role="user",
            content=user_text,
            seq=1,
            created_at=now,
        )
    )
    db.add(
        AgentMessage(
            id=f"msg_a_{sid}",
            session_id=sid,
            role="assistant",
            content=assistant_text,
            seq=2,
            created_at=now,
        )
    )
    db.commit()


# ── 纯函数 ──


@pytest.mark.parametrize(
    "title,expected",
    [
        ("新会话", True),
        ("", True),
        ("   ", True),
        ("未命名会话", True),
        ("新对话", True),
        ("查看分镜图进度", False),
        ("我的自定义名", False),
    ],
)
def test_is_default_title(title, expected):
    assert st.is_default_title(title) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"解析剧本"', "解析剧本"),
        ("《剧本解析》", "剧本解析"),
        ("【生图失败排查】", "生图失败排查"),
        ("标题：排查生图失败", "排查生图失败"),
        ("查看分镜进度。", "查看分镜进度"),
        ("查看分镜进度！", "查看分镜进度"),
        ("  多行标题\n第二行解释  ", "多行标题"),
        ("`代码风格检查`", "代码风格检查"),
    ],
)
def test_clean_title(raw, expected):
    assert st._clean_title(raw) == expected


def test_clean_title_truncates_long_output():
    long_raw = "这是一个非常非常长的会话标题超出了允许的最大长度限制"
    out = st._clean_title(long_raw)
    assert len(out) <= st.MAX_TITLE_LEN


def test_fallback_title_uses_first_non_empty_line():
    assert st._fallback_title("\n\n  排查生图失败  \n更多内容") == "排查生图失败"


def test_fallback_title_truncates_with_ellipsis():
    out = st._fallback_title("一" * 40)
    assert out.endswith("…")
    assert len(out) == 21  # 20 字 + 省略号


def test_fallback_title_on_blank_input():
    assert st._fallback_title("   \n  ") == "新会话"


# ── 服务层 ──


def test_generate_title_from_llm(db_session, monkeypatch):
    """正常路径：LLM 返回标题，写回会话。"""
    sid = "ags_test01"
    _make_session(db_session, sid=sid)
    _add_turns(db_session, sid, "帮我看看分镜图生成到哪一步了")

    class FakeClient:
        def chat(self, *a, **kw):
            return "查看分镜图进度"

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: FakeClient())
    assert st.generate_session_title(sid) == "查看分镜图进度"
    db_session.expire_all()
    assert db_session.get(AgentSession, sid).title == "查看分镜图进度"


def test_generate_title_skips_user_named_session(db_session, monkeypatch):
    """用户手动命名过的会话不得被覆盖。"""
    sid = "ags_test02"
    _make_session(db_session, title="我的自定义名", sid=sid)
    _add_turns(db_session, sid, "随便聊聊")

    called = {"n": 0}

    class FakeClient:
        def chat(self, *a, **kw):
            called["n"] += 1
            return "不应出现"

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: FakeClient())
    assert st.generate_session_title(sid) is None
    assert called["n"] == 0, "已命名会话不应触发 LLM 调用"
    db_session.expire_all()
    assert db_session.get(AgentSession, sid).title == "我的自定义名"


def test_generate_title_falls_back_when_llm_fails(db_session, monkeypatch):
    """LLM 报错时降级为本地摘要，不抛异常。"""
    sid = "ags_test03"
    _make_session(db_session, sid=sid)
    _add_turns(db_session, sid, "排查生图失败的原因")

    class BoomClient:
        def chat(self, *a, **kw):
            raise st.LLMError("llm_unreachable", "boom")

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: BoomClient())
    assert st.generate_session_title(sid) == "排查生图失败的原因"


def test_generate_title_falls_back_on_unexpected_exception(db_session, monkeypatch):
    """非 LLMError 的意外异常同样降级（命名绝不能打断聊天链路）。"""
    sid = "ags_test04"
    _make_session(db_session, sid=sid)
    _add_turns(db_session, sid, "看看任务中心")

    class BoomClient:
        def chat(self, *a, **kw):
            raise RuntimeError("unexpected")

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: BoomClient())
    assert st.generate_session_title(sid) == "看看任务中心"


def test_generate_title_no_user_message(db_session, monkeypatch):
    """空会话（无 user 消息）不命名。"""
    sid = "ags_test05"
    _make_session(db_session, sid=sid)
    assert st.generate_session_title(sid) is None


def test_generate_title_missing_session(db_session):
    assert st.generate_session_title("ags_nope") is None


# ── 端点 ──


def test_auto_title_endpoint_renames_default_session(client, db_session, monkeypatch):
    sid = "ags_test06"
    _make_session(db_session, sid=sid)
    _add_turns(db_session, sid, "帮我解析第 1 集的剧本")

    class FakeClient:
        def chat(self, *a, **kw):
            return "解析第一集剧本"

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: FakeClient())
    r = client.post(f"/api/agent/sessions/{sid}/auto-title")
    assert r.status_code == 200, r.text
    assert r.json()["renamed"] is True
    assert r.json()["title"] == "解析第一集剧本"


def test_auto_title_endpoint_is_idempotent(client, db_session, monkeypatch):
    """第二次调用不再改名（标题已不是默认值）。"""
    sid = "ags_test07"
    _make_session(db_session, sid=sid)
    _add_turns(db_session, sid, "看看资产库")

    class FakeClient:
        def chat(self, *a, **kw):
            return "查看资产库"

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: FakeClient())
    first = client.post(f"/api/agent/sessions/{sid}/auto-title").json()
    assert first["renamed"] is True
    second = client.post(f"/api/agent/sessions/{sid}/auto-title").json()
    assert second["renamed"] is False
    assert second["title"] == first["title"]


def test_auto_title_endpoint_respects_manual_rename(client, db_session, monkeypatch):
    sid = "ags_test08"
    _make_session(db_session, sid=sid)
    _add_turns(db_session, sid, "随便问问")
    client.patch(f"/api/agent/sessions/{sid}", json={"title": "手动命名"})

    class FakeClient:
        def chat(self, *a, **kw):
            return "自动命名"

    monkeypatch.setattr(st, "LLMClient", lambda *a, **kw: FakeClient())
    r = client.post(f"/api/agent/sessions/{sid}/auto-title")
    assert r.json()["renamed"] is False
    assert r.json()["title"] == "手动命名"


def test_auto_title_endpoint_404_for_unknown_session(client):
    r = client.post("/api/agent/sessions/ags_missing/auto-title")
    assert r.status_code == 404
