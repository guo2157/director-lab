"""剧本创作入口测试（阶段1）：AI 创作/扩写剧本端点。

覆盖：
- create/expand 两种模式的提示词组装与配置注入
- LLM 失败按 llm_* 错误码返回，不落库半成品
- 成功后保存为新剧本版本（版本号递增、current_script_version_id 更新）
- 参数校验：mode 非法 400、source_text 缺失 400、归档项目 409
- 提示词外置：script_create_system / script_expand_system 注册并可编辑
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
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


_MOCK_SCRIPT = """第1集

场次1：清晨/马棚

场景描述：木质马棚，铺着干草。

小马：（兴奋地）妈妈，我能把麦子驮到河对岸的磨坊吗？

老马：（温和地）可以，但路上要小心。
"""


def test_script_write_create_saves_new_version(client: TestClient, monkeypatch):
    """create 模式：LLM 生成剧本保存为新剧本版本。"""
    import app.integrations.llm as llm_mod

    class _FakeClient:
        def chat(self, system, user, json_mode=True):
            assert "创意" in user or "创作" in user
            assert "题材：儿童动画" in user
            return _MOCK_SCRIPT

    monkeypatch.setattr(llm_mod, "LLMClient", _FakeClient)

    pid = client.post("/api/projects", json={"name": "创作项目", "aspect_ratio": "9:16"}).json()["id"]
    client.patch(f"/api/projects/{pid}", json={"settings": {"genre": "儿童动画", "episode_count": 1}})

    r = client.post(f"/api/projects/{pid}/script-write", json={
        "mode": "create",
        "source_text": "一个小马帮妈妈送麦子过河的儿童故事",
    })
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 1
    assert "小马" in data["text"]

    cur = client.get(f"/api/projects/{pid}/script-versions/current").json()
    assert cur["script_version_id"] == data["script_version_id"]

    r2 = client.post(f"/api/projects/{pid}/script-write", json={
        "mode": "expand",
        "source_text": "小马过河故事梗概",
    })
    assert r2.status_code == 201
    assert r2.json()["version"] == 2


def test_script_write_llm_failure_no_partial_save(client: TestClient, monkeypatch):
    """LLM 失败：返回 llm_* 错误码，不落库半成品。"""
    from app.integrations.llm import LLMError
    import app.integrations.llm as llm_mod

    class _FailClient:
        def chat(self, system, user, json_mode=True):
            raise LLMError("llm_unreachable", "语言模型连接失败: ConnectError")

    monkeypatch.setattr(llm_mod, "LLMClient", _FailClient)

    pid = client.post("/api/projects", json={"name": "失败项目", "aspect_ratio": "9:16"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/script-write", json={
        "mode": "create", "source_text": "一个创意",
    })
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "llm_unreachable"

    cur = client.get(f"/api/projects/{pid}/script-versions/current").json()
    assert cur["exists"] is False
    assert not cur["text"]


def test_script_write_validation(client: TestClient):
    """参数校验：mode 非法/source_text 缺失 400；归档项目 409。"""
    pid = client.post("/api/projects", json={"name": "校验项目", "aspect_ratio": "9:16"}).json()["id"]

    r = client.post(f"/api/projects/{pid}/script-write", json={"mode": "invalid", "source_text": "x"})
    assert r.status_code == 400
    r = client.post(f"/api/projects/{pid}/script-write", json={"mode": "create", "source_text": ""})
    assert r.status_code == 400
    r = client.post(f"/api/projects/{pid}/script-write", json={"mode": "create"})
    assert r.status_code == 400

    # draft → active → archived 后拒绝创作
    from app.core.database import SessionLocal
    from app.models import Project
    db = SessionLocal()
    try:
        p = db.get(Project, pid)
        p.status = "active"
        db.commit()
    finally:
        db.close()
    client.post(f"/api/projects/{pid}/archive")
    r = client.post(f"/api/projects/{pid}/script-write", json={"mode": "create", "source_text": "x"})
    assert r.status_code == 409


def test_writing_prompts_externalized():
    """提示词外置：script_create_system / script_expand_system 已注册，可读取。"""
    from app.services.prompt_store import get_prompt
    create_p = get_prompt("script_create_system")
    expand_p = get_prompt("script_expand_system")
    assert "短剧编剧" in create_p
    assert "扩写" in expand_p


def test_build_writing_user_prompt_config_injection():
    """创作配置分段注入用户提示词；空配置不输出配置段。"""
    from app.services.script_writing import build_writing_user_prompt
    u = build_writing_user_prompt("expand", "梗概文本", {
        "genre": "悬疑", "audience": "成年观众", "visual_style": "真人",
        "episode_count": 5, "episode_duration_target": 90,
        "language": "中文", "dialogue_style": "快节奏",
        "content_rating": "全年龄", "character_settings": "主角为侦探",
        "worldview": "现代都市", "requirements": "每集结尾留悬念",
    })
    assert "梗概文本" in u
    for frag in ("题材：悬疑", "目标受众：成年观众", "视觉风格：真人", "集数：5",
                 "语言：中文", "对白风格：快节奏", "内容尺度：全年龄",
                 "角色设定：主角为侦探", "世界观：现代都市", "创作要求：每集结尾留悬念"):
        assert frag in u, frag
    u2 = build_writing_user_prompt("create", "创意", {})
    assert "创作配置" not in u2
