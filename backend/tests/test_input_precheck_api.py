"""接口层输入预算预检测试：超长剧本在创建任务前就被拒绝。

为什么要单独测接口层：`precheck_input_budget` 在 service 与 API 两处都调用了。
service 那层是兜底（任务已经进队列），API 这层才是用户实际看到的体验——
必须在**建任务之前**返回 400，不能让用户看到一个「排队 → 失败」的任务。

同时验证正常长度剧本不受影响，避免预检误拦。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
from app.models import Base, Job


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'precheck_api.db'}")
    Base.metadata.create_all(engine)

    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)

    import app.worker.parse_executor as pe_mod
    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)

    yield TestClient(app)
    # sessionmaker 本身没有 close()：engine 随 tmp_path 回收即可，
    # 这里显式 dispose 释放连接池，避免临时库句柄泄漏。
    engine.dispose()


class _TightSettings:
    """把预算压到很小，让任何像样的剧本都超限。"""

    llm_context_window_tokens = 1000
    llm_input_budget_ratio = 1.0
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3


class _RoomSettings:
    llm_context_window_tokens = 1000000
    llm_input_budget_ratio = 0.6
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3


def _project_and_script(client: TestClient, text: str) -> tuple[str, dict]:
    r = client.post("/api/projects", json={"name": "预检接口项目", "aspect_ratio": "9:16"})
    assert r.status_code == 201
    pid = r.json()["id"]
    r = client.post(f"/api/projects/{pid}/script-versions", json={"text": text})
    assert r.status_code == 201
    return pid, r.json()


# 「第X集」必须在行首才是集标记
#
# 关键：必须是「单集正文」就超预算，而不是「整本」超预算。
# precheck_input_budget 是两级判定（见 app/integrations/token_estimate.py）：
#   1. 单集正文超预算 → 必然失败，任何切分手段都救不了 → 拒绝；
#   2. 整本超预算但单集放得下 → allow_segmented=True 时返回 segmented 放行，
#      交给分段大纲处理。
# 早前这里用「2 集 × 500 字」构造，每集 520 字 ≈ 617 token，远低于 1000 预算，
# 属于第 2 种情况，预检正确放行，却与「应被拒绝」的断言冲突——是测试数据错了，
# 不是实现错了。要验证拒绝分支，必须让**单集**自己就放不下。
LONG_SCRIPT = f"第1集 标题\n场次1 夜 室内\n林昭：{'戏' * 1200}"
SHORT_SCRIPT = "第1集 标题\n场次1 夜 室内\n林昭：走吧。"


def test_parse_rejects_overlong_script_before_creating_job(client, monkeypatch):
    monkeypatch.setattr("app.api.v1.script.get_settings", lambda: _TightSettings())
    pid, sv = _project_and_script(client, LONG_SCRIPT)

    r = client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": LONG_SCRIPT,
        "episode_start": 1,
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "llm_input_too_long"
    assert "token" in detail["message"]


def test_parse_overlong_does_not_persist_any_job(client, monkeypatch):
    """关键：拒绝必须发生在建任务之前，不能留下一个注定失败的任务。"""
    monkeypatch.setattr("app.api.v1.script.get_settings", lambda: _TightSettings())
    pid, sv = _project_and_script(client, LONG_SCRIPT)

    client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": LONG_SCRIPT,
        "episode_start": 1,
    })

    from app.core.database import SessionLocal
    session = SessionLocal()
    try:
        assert session.query(Job).count() == 0
    finally:
        session.close()


def test_script_agent_rejects_overlong_script_before_creating_job(client, monkeypatch):
    monkeypatch.setattr("app.api.v1.script.get_settings", lambda: _TightSettings())
    pid, sv = _project_and_script(client, LONG_SCRIPT)

    r = client.post(f"/api/projects/{pid}/script-agent", json={
        "script_version_id": sv["script_version_id"], "text": LONG_SCRIPT,
        "episode_start": 1,
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "llm_input_too_long"

    from app.core.database import SessionLocal
    session = SessionLocal()
    try:
        assert session.query(Job).count() == 0
    finally:
        session.close()


def test_short_script_passes_precheck_and_creates_job(client, monkeypatch):
    """对照组：正常长度剧本不能被误拦。"""
    monkeypatch.setattr("app.api.v1.script.get_settings", lambda: _RoomSettings())
    pid, sv = _project_and_script(client, SHORT_SCRIPT)

    r = client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": SHORT_SCRIPT,
        "episode_start": 1,
    })
    # 预检通过后进入正常流程；此处只关心不是 400 预检拒绝
    assert r.status_code != 400, r.text

    from app.core.database import SessionLocal
    session = SessionLocal()
    try:
        assert session.query(Job).count() >= 1
    finally:
        session.close()
