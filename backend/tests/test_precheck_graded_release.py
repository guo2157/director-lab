"""端点输入预算预检的分级放行语义（阶段 3 后续）。

背景：预检最初在端点是「一律硬拒」，把「整本超预算、单集放得下」的剧本也挡在
建任务之前——而这恰恰是分段大纲要解决的场景，用户本可正常解析却被要求先手动拆集。

分级放行后的三态：
1. 单集正文超预算 → 400 llm_input_too_long（任何切分都救不回来）
2. 仅整本超预算、单集放得下 → 202 建任务，outline_mode=segmented
3. 整本都在预算内 → 202 建任务，outline_mode=full

预算口径（_TightSettings：window=1000、ratio=1.0、cjk=1.2）：
- 500 汉字/集 ≈ 617 token；2 集整本 ≈ 1234 token
- 1000 汉字/集 ≈ 1217 token（单集即超预算）
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.database import make_engine
from app.models import Base, Job


class _TightSettings:
    """预算 1000 token。"""

    llm_context_window_tokens = 1000
    llm_input_budget_ratio = 1.0
    llm_cjk_token_factor = 1.2
    llm_other_token_factor = 0.3


# 每集 500 汉字 ≈ 617 token：单集放得下，2 集合计 ≈ 1234 token 放不下
WHOLE_OVER_ONLY = "\n".join(
    f"第{i}集 标题\n场次1 夜 室内\n林昭：{'戏' * 500}" for i in range(1, 3)
)
# 单集 1000 汉字 ≈ 1217 token：单集本身就超预算
SINGLE_OVER = f"第1集 标题\n场次1 夜 室内\n林昭：{'戏' * 1000}"
# 正常短剧本
SHORT = "第1集 标题\n场次1 夜 室内\n林昭：走吧。"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'graded_release.db'}")
    Base.metadata.create_all(engine)

    import app.core.database as db_mod
    from sqlalchemy.orm import sessionmaker
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)

    import app.worker.parse_executor as pe_mod
    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    monkeypatch.setattr("app.api.v1.script.get_settings", lambda: _TightSettings())

    yield TestClient(app)
    # sessionmaker 没有 close()，连接由 engine 统一释放
    engine.dispose()


def _project_and_script(client: TestClient, text: str) -> tuple[str, dict]:
    r = client.post("/api/projects", json={"name": "分级放行项目", "aspect_ratio": "9:16"})
    assert r.status_code == 201
    pid = r.json()["id"]
    r = client.post(f"/api/projects/{pid}/script-versions", json={"text": text})
    assert r.status_code == 201
    return pid, r.json()


def _jobs():
    from app.core.database import SessionLocal

    session = SessionLocal()
    try:
        return session.query(Job).all()
    finally:
        session.close()


# ---------------------------------------------------- 态 1：单集超预算必拒
def test_parse_single_episode_over_budget_rejected(client):
    pid, sv = _project_and_script(client, SINGLE_OVER)
    r = client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": SINGLE_OVER,
        "episode_start": 1,
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "llm_input_too_long"
    # 提示必须指向「单集」而不是笼统的整本超限，用户才知道该拆哪一集
    assert "第 1 集" in detail["message"] or "第1集" in detail["message"]
    assert _jobs() == []


def test_script_agent_single_episode_over_budget_rejected(client):
    pid, sv = _project_and_script(client, SINGLE_OVER)
    r = client.post(f"/api/projects/{pid}/script-agent", json={
        "script_version_id": sv["script_version_id"], "text": SINGLE_OVER,
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "llm_input_too_long"
    assert _jobs() == []


# ------------------------------------ 态 2：仅整本超预算 → 放行并标记 segmented
def test_parse_whole_over_budget_is_released_as_segmented(client):
    pid, sv = _project_and_script(client, WHOLE_OVER_ONLY)
    r = client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": WHOLE_OVER_ONLY,
        "episode_start": 1,
    })
    assert r.status_code == 202, r.text

    jobs = _jobs()
    assert len(jobs) == 1
    # 预检结论要随任务留下来：排障时能直接看出这次为什么走分段
    assert (jobs[0].params or {}).get("outline_mode") == "segmented"


def test_script_agent_whole_over_budget_is_released_as_segmented(client):
    pid, sv = _project_and_script(client, WHOLE_OVER_ONLY)
    r = client.post(f"/api/projects/{pid}/script-agent", json={
        "script_version_id": sv["script_version_id"], "text": WHOLE_OVER_ONLY,
    })
    assert r.status_code == 202, r.text

    jobs = _jobs()
    assert len(jobs) == 1
    assert (jobs[0].params or {}).get("outline_mode") == "segmented"


# ------------------------------------------- 态 3：正常长度 → full，不受影响
def test_parse_within_budget_marks_full_mode(client):
    pid, sv = _project_and_script(client, SHORT)
    r = client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": SHORT,
        "episode_start": 1,
    })
    assert r.status_code == 202, r.text

    jobs = _jobs()
    assert len(jobs) == 1
    assert (jobs[0].params or {}).get("outline_mode") == "full"


# --------------------------------- 无集标记：单集≈整本，无法分段，必须硬拒
def test_parse_unmarked_over_budget_rejected(client):
    unmarked = "林昭：" + "戏" * 1200  # 无「第X集」标记，无法按集切窗
    pid, sv = _project_and_script(client, unmarked)
    r = client.post(f"/api/projects/{pid}/parse", json={
        "script_version_id": sv["script_version_id"], "text": unmarked,
        "episode_start": 1,
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["code"] == "llm_input_too_long"
    assert "无集标记" in detail["message"]
    assert _jobs() == []
