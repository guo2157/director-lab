"""项目风格配置 API 集成测试：默认值、读取、保存校验与旧键兼容。"""


from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.core.database import make_engine
from app.models import Base


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 与既有集成测试一致：独立 sqlite 文件 + 替换 SessionLocal
    engine = make_engine(f"sqlite:///{tmp_path / "style.db"}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def _create(client):
    r = client.post("/api/projects", json={"name": "风格配置测试", "aspect_ratio": "9:16"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_new_project_has_default_style_config(client):
    from app.services import style_config
    pid = _create(client)
    body = client.get(f"/api/projects/{pid}/style-config").json()
    assert body["config"]["visual_style"] == style_config.DEFAULT_VISUAL_STYLE
    assert body["version"] == style_config.STYLE_CONFIG_VERSION
    assert body["issues"] == []
    assert body["visual_style_label"] == style_config.VISUAL_STYLES[style_config.DEFAULT_VISUAL_STYLE]["label"]


def test_style_options_endpoint_lists_values(client):
    r = client.get("/api/projects/style-options")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visual_styles"] and body["genre_tags"]
    assert body["defaults"]["visual_style"] == body["defaults"]["visual_style"]


def test_save_style_config_persists_and_normalizes(client):
    pid = _create(client)
    payload = {"config": {
        "genre_tags": ["童话故事", "寓言故事"],
        "narrative_styles": ["寓教于乐型", "温情治愈型"],
        "visual_style": "3d_cartoon",
        "pacing": "gentle",
        "emotional_tone": ["温馨", "轻松"],
        "conflict_intensity": "moderate",
        "twist_density": "occasional",
        "dialogue_style": "child_friendly",
        "narration_mode": "mixed",
        "target_audience": "kindergarten_primary_2",
        "custom_style_requirements": "角色造型适合儿童观看",
    }}
    r = client.put(f"/api/projects/{pid}/style-config", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["saved"] is True
    body = client.get(f"/api/projects/{pid}/style-config").json()
    assert body["config"]["visual_style"] == "3d_cartoon"
    assert body["config"]["genre_tags"] == ["童话故事", "寓言故事"]
    assert body["config"]["custom_style_requirements"] == "角色造型适合儿童观看"
    project = client.get(f"/api/projects/{pid}").json()
    assert project["settings"]["visual_style"] == "3d_cartoon"
    assert project["settings"]["style_config_version"]


def test_save_invalid_visual_style_reports_issue_and_resets(client):
    from app.services import style_config
    pid = _create(client)
    r = client.put(f"/api/projects/{pid}/style-config",
                   json={"config": {"visual_style": "不存在的风格"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["visual_style"] == style_config.DEFAULT_VISUAL_STYLE
    assert any(i["code"] == "visual_style_unknown" for i in body["issues"])


def test_patch_settings_with_style_fields_normalizes_legacy_keys(client):
    pid = _create(client)
    r = client.patch(f"/api/projects/{pid}", json={"settings": {
        "genre": "穿越", "visual_style": "国风水墨"}})
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert settings["visual_style"] == "chinese_ink"
    assert settings["genre_tags"] == ["穿越"]


def test_patch_settings_keeps_non_style_keys(client):
    pid = _create(client)
    r = client.patch(f"/api/projects/{pid}", json={"settings": {
        "episode_duration_target": 120, "visual_style": "comic"}})
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert settings["episode_duration_target"] == 120
    assert settings["visual_style"] == "comic"


# ── 修复回归：PUT 部分更新不得覆盖未提交字段 ──────────────────
def _save(client, pid, config: dict) -> dict:
    r = client.put(f"/api/projects/{pid}/style-config", json={"config": config})
    assert r.status_code == 200, r.text
    return r.json()


def test_put_partial_update_keeps_existing_fields(client):
    """只提交部分字段时，此前保存的其他字段必须保留，不得被默认值覆盖。"""
    pid = _create(client)
    _save(client, pid, {
        "genre_tags": ["童话故事"],
        "narrative_styles": ["寓教于乐型"],
        "pacing": "gentle",
        "emotional_tone": ["温馨", "轻松"],
        "visual_style": "3d_cartoon",
        "custom_style_requirements": "造型适合儿童",
    })
    # 仅改视觉风格
    _save(client, pid, {"visual_style": "anime_2d"})
    body = client.get(f"/api/projects/{pid}/style-config").json()["config"]
    assert body["visual_style"] == "anime_2d"
    assert body["genre_tags"] == ["童话故事"]
    assert body["narrative_styles"] == ["寓教于乐型"]
    assert body["pacing"] == "gentle"
    assert body["emotional_tone"] == ["温馨", "轻松"]
    assert body["custom_style_requirements"] == "造型适合儿童"


def test_create_project_with_style_config(client):
    """新建项目时可一并提交风格配置，落库后即生效且非风格键保留默认。"""
    r = client.post("/api/projects", json={
        "name": "带风格的新项目",
        "aspect_ratio": "16:9",
        "style_config": {
            "genre_tags": ["童话故事"],
            "visual_style": "clay_stop_motion",
            "pacing": "gentle",
            "emotional_tone": ["温馨"],
            "custom_style_requirements": "黏土质感明确",
        },
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    body = client.get(f"/api/projects/{pid}/style-config").json()["config"]
    assert body["visual_style"] == "clay_stop_motion"
    assert body["genre_tags"] == ["童话故事"]
    assert body["pacing"] == "gentle"
    assert body["custom_style_requirements"] == "黏土质感明确"
    # 未提交字段取默认值
    assert body["dialogue_style"] == "natural"


def test_create_project_without_style_config_uses_defaults(client):
    """不提交风格配置时，创建结果与纯默认基线一致。"""
    from app.services import style_config
    r = client.post("/api/projects", json={"name": "默认风格项目", "aspect_ratio": "9:16"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    body = client.get(f"/api/projects/{pid}/style-config").json()["config"]
    assert body == style_config.default_style_config()


def test_create_project_with_invalid_style_value_normalizes(client):
    """新建时提交非法视觉风格，落库为默认值而非原样写入。"""
    from app.services import style_config
    r = client.post("/api/projects", json={
        "name": "非法风格项目",
        "aspect_ratio": "1:1",
        "style_config": {"visual_style": "不存在的风格"},
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    body = client.get(f"/api/projects/{pid}/style-config").json()["config"]
    assert body["visual_style"] == style_config.DEFAULT_VISUAL_STYLE


def test_put_unknown_enum_value_reports_issue(client):
    """提交非法枚举值时返回问题码并回退默认，而不是静默接受。"""
    pid = _create(client)
    res = _save(client, pid, {"dialogue_style": "对白风格"})
    codes = {i["code"] for i in res["issues"]}
    assert "dialogue_style_unknown" in codes
    assert res["config"]["dialogue_style"] == "natural"
