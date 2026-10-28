"""导演台 Agent 全流程端到端测试。
覆盖（不依赖 LLM，生成请求走意图规则快路径）：
- 镜头目标链路：chat『为第 N 镜生成关键帧』→ 审批记录 → 批准 → 执行 → 任务创建（D-23 门禁）
- clip 链路：D-13 闸口拦截（分镜图未确认）与放行（确认后）
- 实体目标链路：chat 含 chr_ ID → 实体生成申请 → 批准执行 → 任务参数含归属键与模板提示词 → 审核状态回退 pending
- 异常路径：未关联项目、跨项目实体、重复裁决 409、重复执行幂等复用、未批准执行被拦
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import Base


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "e2e.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


@pytest.fixture()
def project_env(client, tmp_path):
    """完整项目环境：项目（审核通过）+ 场景（含基准图）+ 镜头 + 角色 + image/video 工作流。"""
    from app.models import Character, Project, Scene, Shot, Workflow, ids
    from app.models import Asset
    import app.core.database as db_mod

    session = db_mod.SessionLocal()
    now = datetime.now()
    project = Project(
        id=ids.project_id(),
        name="端到端项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
        assets_review_status="approved",
    )
    session.add(project)
    session.commit()

    scene = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1, title="场景一")
    session.add(scene)
    session.commit()

    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    baseline_file = media / "baseline.png"
    baseline_file.write_bytes(b"x")
    baseline = Asset(
        id=ids.asset_id(),
        project_id=project.id,
        asset_type="scene",
        name="场景一基准图",
        version=1,
        file_path=str(baseline_file),
        validity="current",
    )
    scene.baseline_asset_id = baseline.id
    session.add(baseline)

    portrait_file = media / "portrait.png"
    portrait_file.write_bytes(b"x")
    portrait = Asset(
        id=ids.asset_id(),
        project_id=project.id,
        asset_type="character",
        name="主角定妆图",
        version=1,
        file_path=str(portrait_file),
        validity="current",
    )
    session.add(portrait)
    session.flush()

    shot = Shot(
        id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1, shot_kind="normal", duration=3
    )
    character = Character(
        id=ids.character_id(),
        project_id=project.id,
        name="主角",
        appearance="黑发少年，穿蓝色外套",
        description="故事的主人公",
    )
    image_wf = Workflow(
        id=ids.workflow_id(),
        name="默认图像工作流",
        workflow_type="image",
        is_default=True,
        is_builtin=True,
        version=1,
        definition={},
    )
    video_wf = Workflow(
        id=ids.workflow_id(),
        name="默认视频工作流",
        workflow_type="video",
        is_default=True,
        is_builtin=True,
        version=1,
        definition={},
    )
    tts_wf = Workflow(
        id=ids.workflow_id(),
        name="默认语音工作流",
        workflow_type="tts",
        is_default=True,
        is_builtin=True,
        version=1,
        definition={},
    )
    session.add_all([shot, character, image_wf, video_wf, tts_wf])
    session.flush()
    character.portrait_asset_id = portrait.id
    session.commit()
    out = {
        "project_id": project.id,
        "scene_id": scene.id,
        "shot_id": shot.id,
        "character_id": character.id,
    }
    session.close()
    return out


def _make_session(client, project_id=None):
    payload = {"title": "端到端会话"}
    if project_id:
        payload["project_id"] = project_id
    r = client.post("/api/agent/sessions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _chat(client, sid, content):
    r = client.post(f"/api/agent/sessions/{sid}/chat", json={"content": content})
    assert r.status_code == 200, r.text
    return r.json()


def test_e2e_keyframe_flow(client, project_env):
    """镜头目标全链路：chat 生成请求 → 审批 → 批准 → 执行 → 任务创建。"""
    sid = _make_session(client, project_env["project_id"])

    # 1. chat 发起生成请求（规则快路径命中，无需 LLM）
    resp = _chat(client, sid, f"为第 1 镜生成关键帧")
    assistant = resp["assistant_message"]
    approval = (assistant.get("payload") or {}).get("approval") or {}
    assert approval.get("ok") is True, assistant["content"]
    confirmation_id = approval["confirmation_id"]

    # 2. 审批面板可见待审记录
    r = client.get(f"/api/agent/sessions/{sid}/approvals")
    assert r.status_code == 200
    assert any(c["confirmation_id"] == confirmation_id for c in r.json()["items"])

    # 3. 未批准先执行被拦（409/400，不建任务）
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{confirmation_id}/execute")
    assert r.status_code in (400, 409)

    # 4. 批准
    r = client.post(
        f"/api/agent/sessions/{sid}/approvals/{confirmation_id}/resolve",
        json={"approve": True, "comment": "同意"},
    )
    assert r.status_code == 200, r.text

    # 5. 执行 → 任务创建
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{confirmation_id}/execute")
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["reused"] is False

    # 6. 重复执行幂等复用
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{confirmation_id}/execute")
    assert r.status_code == 200
    assert r.json()["job_id"] == job_id
    assert r.json()["reused"] is True

    # 7. 重复裁决被拒（409）
    r = client.post(
        f"/api/agent/sessions/{sid}/approvals/{confirmation_id}/resolve",
        json={"approve": True},
    )
    assert r.status_code == 409


def test_e2e_clip_blocked_then_allowed(client, project_env):
    """clip 链路：D-13 闸口先拦截，分镜图确认后放行。"""
    from app.models import Shot
    import app.core.database as db_mod

    sid = _make_session(client, project_env["project_id"])

    # 分镜图未确认：申请可建，执行被 D-13 拦下
    resp = _chat(client, sid, "为第 1 镜生成视频")
    approval = (resp["assistant_message"].get("payload") or {}).get("approval") or {}
    assert approval.get("ok") is True, resp["assistant_message"]["content"]
    cid = approval["confirmation_id"]
    client.post(
        f"/api/agent/sessions/{sid}/approvals/{cid}/resolve",
        json={"approve": True},
    )
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{cid}/execute")
    assert r.status_code == 400, r.text
    assert "keyframe_not_confirmed" in r.text

    # 确认分镜图后重新走链路：放行
    session = db_mod.SessionLocal()
    shot = session.get(Shot, project_env["shot_id"])
    shot.keyframe_confirmed = True
    session.commit()
    session.close()

    resp = _chat(client, sid, "为第 1 镜生成视频")
    approval = (resp["assistant_message"].get("payload") or {}).get("approval") or {}
    assert approval.get("ok") is True
    cid2 = approval["confirmation_id"]
    client.post(
        f"/api/agent/sessions/{sid}/approvals/{cid2}/resolve",
        json={"approve": True},
    )
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{cid2}/execute")
    assert r.status_code == 200, r.text
    assert r.json()["job_type"] == "generate_clip"


def test_e2e_entity_flow(client, project_env):
    """实体目标全链路：chat 含实体 ID → 申请 → 批准执行 → 归属键与模板提示词 → 审核回退。"""
    from app.models import Project
    import app.core.database as db_mod

    sid = _make_session(client, project_env["project_id"])
    ch_id = project_env["character_id"]

    resp = _chat(client, sid, f"为角色 {ch_id} 生成角色图")
    approval = (resp["assistant_message"].get("payload") or {}).get("approval") or {}
    assert approval.get("ok") is True, resp["assistant_message"]["content"]
    cid = approval["confirmation_id"]

    client.post(
        f"/api/agent/sessions/{sid}/approvals/{cid}/resolve",
        json={"approve": True},
    )
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{cid}/execute")
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # 任务参数：归属键 + 模板提示词（非空）
    from app.models import Job

    session = db_mod.SessionLocal()
    job = session.get(Job, job_id)
    assert job.params.get("__character_id__") == ch_id
    assert job.shot_id is None
    assert job.params.get("prompt"), "实体生成空 prompt 未兜底"
    assert "主角" in job.params["prompt"]

    # D-23：公共资产重新生成后审核状态回退 pending
    project = session.get(Project, project_env["project_id"])
    assert project.assets_review_status == "pending"
    session.close()


def test_e2e_no_project_session(client, project_env):
    """未关联项目的会话：生成请求返回引导提示，不建审批。"""
    sid = _make_session(client)
    resp = _chat(client, sid, "为第 1 镜生成关键帧")
    assistant = resp["assistant_message"]
    approval = (assistant.get("payload") or {}).get("approval")
    assert approval is None
    assert "未关联项目" in assistant["content"]


def test_e2e_foreign_entity_rejected(client, project_env):
    """跨项目实体：chat 提示不属于当前项目，不建审批。"""
    from app.models import Character, Project
    import app.core.database as db_mod

    session = db_mod.SessionLocal()
    now = datetime.now()
    other = Project(
        id="prj_otherproject01",
        name="别的项目",
        aspect_ratio="9:16",
        creation_type="script",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(other)
    session.commit()
    foreign = Character(id="chr_foreignchar01", project_id=other.id, name="别人角色")
    session.add(foreign)
    session.commit()
    session.close()

    sid = _make_session(client, project_env["project_id"])
    resp = _chat(client, sid, f"为角色 chr_foreignchar01 生成角色图")
    assistant = resp["assistant_message"]
    approval = (assistant.get("payload") or {}).get("approval")
    assert approval is None
    assert "不属于当前项目" in assistant["content"]


def test_e2e_shot_prompt_fallback_and_tts(client, project_env):
    """镜头目标参数组装：keyframe 空 prompt 兜底镜头提示词；audio 走 tts_text（台词）。"""
    from app.models import Job, Shot
    import app.core.database as db_mod

    session = db_mod.SessionLocal()
    shot = session.get(Shot, project_env["shot_id"])
    shot.image_prompt = "夜晚的城市街道，霓虹灯"
    shot.video_prompt = "镜头缓慢推进"
    shot.dialogue = "今天天气不错"
    session.commit()
    session.close()

    sid = _make_session(client, project_env["project_id"])

    # keyframe：未提供 prompt 时兜底 shot.image_prompt
    resp = _chat(client, sid, "为第 1 镜生成关键帧")
    approval = (resp["assistant_message"].get("payload") or {}).get("approval") or {}
    cid = approval["confirmation_id"]
    client.post(f"/api/agent/sessions/{sid}/approvals/{cid}/resolve", json={"approve": True})
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{cid}/execute")
    assert r.status_code == 200, r.text

    session = db_mod.SessionLocal()
    job = session.get(Job, r.json()["job_id"])
    # 兜底镜头提示词（P2 资产清单注入会在头部补 @引用，属预期行为）
    assert "夜晚的城市街道，霓虹灯" in (job.params.get("prompt") or "")
    session.close()

    # audio：参数走 tts_text（台词），不是图像提示词
    resp = _chat(client, sid, "为第 1 镜生成配音")
    approval = (resp["assistant_message"].get("payload") or {}).get("approval") or {}
    assert approval.get("ok") is True, resp["assistant_message"]["content"]
    cid2 = approval["confirmation_id"]
    client.post(f"/api/agent/sessions/{sid}/approvals/{cid2}/resolve", json={"approve": True})
    r = client.post(f"/api/agent/sessions/{sid}/approvals/{cid2}/execute")
    assert r.status_code == 200, r.text

    session = db_mod.SessionLocal()
    job2 = session.get(Job, r.json()["job_id"])
    assert job2.params.get("tts_text") == "今天天气不错"
    session.close()


def test_e2e_repeat_request_reuses_confirmation(client, project_env):
    """同会话同入参重复申请：复用同一条待审记录（幂等）。"""
    sid = _make_session(client, project_env["project_id"])
    resp1 = _chat(client, sid, "为第 1 镜生成关键帧")
    resp2 = _chat(client, sid, "为第 1 镜生成关键帧")
    a1 = (resp1["assistant_message"].get("payload") or {}).get("approval") or {}
    a2 = (resp2["assistant_message"].get("payload") or {}).get("approval") or {}
    assert a1.get("ok") and a2.get("ok")
    assert a1["confirmation_id"] == a2["confirmation_id"]
