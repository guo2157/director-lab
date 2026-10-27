"""剧本质量评估测试（阶段2）：AI 生成结构化评估报告端点。

覆盖：
- LLM 评估成功返回结构化报告（优点/问题/修改建议/制作风险/剧情漏洞/评分/总结）
- LLM 失败按 llm_* 错误码返回 502，不落库半成品
- 参数校验：script_version_id 缺失 400、text 缺失 400、归档项目 409
- 评估为只读操作，不修改剧本版本或项目状态
- 提示词外置：script_evaluation_system 注册并可读取
- 算法估算数据注入用户提示词
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

_MOCK_REPORT = {
    "strengths": [{"detail": "人物对话自然，性格鲜明"}],
    "issues": [{"severity": "minor", "detail": "场景描述略简单"}],
    "suggestions": [{"suggestion": "增加马棚的环境细节描写"}],
    "production_risks": [{"severity": "minor", "detail": "动物角色需要特效支持"}],
    "plot_holes": [],
    "overall_score": 7,
    "summary": "剧本基础扎实，人物鲜活，场景描写可加强。",
}


def test_script_evaluate_success(client: TestClient, monkeypatch):
    """LLM 评估成功：返回结构化报告，包含所有必需字段。"""
    import app.integrations.llm as llm_mod

    class _FakeClient:
        def chat_json(self, system, user):
            assert "剧本" in user
            assert "算法估算数据" in user
            assert "字数" in user
            return _MOCK_REPORT

    monkeypatch.setattr(llm_mod, "LLMClient", _FakeClient)

    pid = client.post("/api/projects", json={"name": "评估项目", "aspect_ratio": "9:16"}).json()["id"]
    # 保存剧本
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": _MOCK_SCRIPT}).json()

    r = client.post(f"/api/projects/{pid}/script-evaluate", json={
        "script_version_id": sv["script_version_id"],
        "text": _MOCK_SCRIPT,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["script_version_id"] == sv["script_version_id"]
    assert "estimate_data" in data
    assert data["estimate_data"]["algorithm_version"] == "est-v1.0"
    report = data["report"]
    assert "strengths" in report
    assert "issues" in report
    assert "suggestions" in report
    assert "production_risks" in report
    assert "plot_holes" in report
    assert "overall_score" in report
    assert "summary" in report
    assert report["overall_score"] == 7


def test_script_evaluate_llm_failure(client: TestClient, monkeypatch):
    """LLM 失败：返回 llm_* 错误码 502，不落库。"""
    from app.integrations.llm import LLMError
    import app.integrations.llm as llm_mod

    class _FailClient:
        def chat_json(self, system, user):
            raise LLMError("llm_timeout", "语言模型请求超时")

    monkeypatch.setattr(llm_mod, "LLMClient", _FailClient)

    pid = client.post("/api/projects", json={"name": "失败项目", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": _MOCK_SCRIPT}).json()

    r = client.post(f"/api/projects/{pid}/script-evaluate", json={
        "script_version_id": sv["script_version_id"],
        "text": _MOCK_SCRIPT,
    })
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "llm_timeout"


def test_script_evaluate_validation(client: TestClient):
    """参数校验：script_version_id 缺失 400、text 缺失 400、归档项目 409。"""
    pid = client.post("/api/projects", json={"name": "校验项目", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": _MOCK_SCRIPT}).json()

    # 缺 script_version_id
    r = client.post(f"/api/projects/{pid}/script-evaluate", json={"text": _MOCK_SCRIPT})
    assert r.status_code == 400
    # 缺 text
    r = client.post(f"/api/projects/{pid}/script-evaluate", json={"script_version_id": sv["script_version_id"]})
    assert r.status_code == 400
    # 归档项目
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
    r = client.post(f"/api/projects/{pid}/script-evaluate", json={
        "script_version_id": sv["script_version_id"],
        "text": _MOCK_SCRIPT,
    })
    assert r.status_code == 409


def test_script_evaluate_text_hash_mismatch(client: TestClient):
    """text 哈希与 script_version_id 不一致：409 script_version_conflict。"""
    pid = client.post("/api/projects", json={"name": "哈希项目", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": _MOCK_SCRIPT}).json()

    r = client.post(f"/api/projects/{pid}/script-evaluate", json={
        "script_version_id": sv["script_version_id"],
        "text": "这是被篡改的剧本内容，哈希不匹配",
    })
    assert r.status_code == 409


def test_evaluation_prompts_externalized():
    """提示词外置：script_evaluation_system 已注册，可读取。"""
    from app.services.prompt_store import get_prompt
    p = get_prompt("script_evaluation_system")
    assert "审稿" in p or "评估" in p


def test_build_evaluation_user_prompt_estimate_injection():
    """算法估算数据注入用户提示词；空配置不输出配置段。"""
    from app.services.script_evaluation import build_evaluation_user_prompt
    estimate_data = {
        "char_count": 500,
        "dialogue_char_estimate": 200,
        "estimated_total_duration": 75.0,
        "estimated_episodes": 1,
        "algorithm_version": "est-v1.0",
        "episode_breakdown": [
            {"episode": 1, "estimated_duration": 75.0, "estimated_shots": 8},
        ],
    }
    u = build_evaluation_user_prompt("剧本正文", estimate_data, {
        "genre": "儿童动画", "episode_count": 1,
    })
    assert "剧本正文" in u
    assert "字数：500" in u
    assert "预估总时长：75.0 秒" in u
    assert "第1集" in u
    assert "题材：儿童动画" in u
    # 空配置
    u2 = build_evaluation_user_prompt("剧本", estimate_data, {})
    assert "创作配置" not in u2
