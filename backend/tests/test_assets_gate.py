"""统一资产就绪门禁测试（D-23 强化）。

核心规则：所有必需角色、场景、道具均已生成完成，并且人工审核通过后，
才允许开始分镜图生成。

覆盖场景（任务书 §12）：
1. 全部生成并审核通过 → 可创建分镜图任务
2. 缺少必需角色 → 不能创建
3. 缺少必需场景 → 不能创建
4. 缺少必需道具 → 不能创建
5. 任一必需资产仍在生成 → 不能创建
6. 任一必需资产待审核 → 不能创建
7. 任一必需资产审核驳回 → 不能创建
8. 资产版本失效 → 不能使用该版本生成
9. 审核通过后重新生成 → 审核状态回退，重新拦截
10. 直接调用后端 API（批量通道）不能绕过门禁
11. Agent 发起的任务不能绕过门禁
12. 执行器二次校验（入队后资产状态变化）
"""
import json
from datetime import datetime, timezone

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
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


_WORKFLOW_DEF = {
    "1": {
        "class_type": "EmptyLatentImage",
        "_meta": {"title": "$~width, $~height"},
        "inputs": {"width": 512, "height": 288, "batch_size": 1},
    },
    "2": {
        "class_type": "SaveImage",
        "_meta": {"title": "$output.cover"},
        "inputs": {"images": ["1", 0]},
    },
}


def _make_project(client: TestClient) -> str:
    r = client.post("/api/projects", json={"name": "门禁项目", "aspect_ratio": "9:16"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _add_entities(client: TestClient, project_id: str) -> dict:
    """创建角色/场景/道具实体与首个镜头（镜头挂在已建场景下，避免产生第二个无基准图的场景）。"""
    from app.core.database import SessionLocal
    from app.models import Character, Prop, Scene, SceneEpisode

    db = SessionLocal()
    try:
        ch = Character(id="chr_test_0001", project_id=project_id, name="主角")
        sc = Scene(id="scn_test_0001", project_id=project_id, seq=1, title="教室")
        db.add_all([ch, sc])
        # 场景-集出场记录：创建镜头的前置条件（shots.py 校验 SceneEpisode 存在）
        db.add(SceneEpisode(scene_id=sc.id, episode=1, seq_in_episode=1))
        pr = Prop(id="prp_test_0001", project_id=project_id, name="宝剑", anchor_required=True)
        db.add(pr)
        db.commit()
        return {"character": ch.id, "scene": sc.id, "prop": pr.id}
    finally:
        db.close()


def _make_shot(client: TestClient, scene_id: str) -> str:
    shot = client.post(
        "/api/shots",
        json={"scene_id": scene_id, "episode": 1, "shot_kind": "visual", "duration": 5},
    ).json()
    return shot["id"]


def _make_workflow(client: TestClient, name: str) -> str:
    r = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": "image"},
        files={"file": (f"{name}.json", json.dumps(_WORKFLOW_DEF), "application/json")},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_asset(db, project_id: str, asset_type: str, name: str, file_path: str, validity: str = "current") -> str:
    """直接落库一个资产并返回 id（绕过生成流程，用于门禁状态构造）。"""
    from app.models import Asset, ids

    a = Asset(
        id=ids.asset_id(),
        project_id=project_id,
        asset_type=asset_type,
        name=name,
        version=1,
        file_path=file_path,
        validity=validity,
    )
    db.add(a)
    db.commit()
    return a.id


def _bind_assets(db, project_id: str, tmp_path, entities: dict, skip: set[str] = set()) -> None:
    """为实体生成有效资产文件并绑定指针；skip 指定不绑定的类型（character/scene/prop）。"""
    from app.models import Character, Prop, Scene

    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    if "character" not in skip:
        f1 = media / "portrait.png"
        f1.write_bytes(b"x")
        ch = db.get(Character, entities["character"])
        ch.portrait_asset_id = _make_asset(db, project_id, "character", "主角", str(f1))
    if "scene" not in skip:
        f2 = media / "baseline.png"
        f2.write_bytes(b"x")
        sc = db.get(Scene, entities["scene"])
        sc.baseline_asset_id = _make_asset(db, project_id, "scene", "教室", str(f2))
    if "prop" not in skip:
        f3 = media / "anchor.png"
        f3.write_bytes(b"x")
        pr = db.get(Prop, entities["prop"])
        pr.anchor_asset_id = _make_asset(db, project_id, "prop", "宝剑", str(f3))
    db.commit()


def _with_db(fn):
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def _approve_review(client: TestClient, project_id: str):
    return client.post(f"/api/projects/{project_id}/assets-review", json={"action": "approve"})


def _generate_keyframe(client: TestClient, shot_id: str, workflow_id: str):
    return client.post(
        f"/api/shots/{shot_id}/generate",
        json={"target": "keyframe", "workflow_id": workflow_id},
    )


# --- 场景 1：全部生成并审核通过 → 放行 ---


def test_gate_passes_when_all_ready(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    r = _approve_review(client, pid)
    assert r.status_code == 200, r.text
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-pass")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 202, r.text


# --- 场景 2/3/4：缺少必需角色/场景/道具 → 拦截 ---


@pytest.mark.parametrize("skip,expect_type", [
    ("character", "character"),
    ("scene", "scene"),
    ("prop", "prop"),
])
def test_gate_blocks_missing_required(client: TestClient, tmp_path, skip, expect_type):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities, skip={skip})

    _with_db(_fill)
    # 审核接口同样拦截（资产不完整时不可审核通过）
    r = _approve_review(client, pid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, f"gate-m-{expect_type}")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    blockers = r.json()["detail"].get("blockers", [])
    assert any(b["asset_type"] == expect_type and b["reason"] == "asset_missing" for b in blockers)


# --- 场景 5：必需资产仍在生成 → 拦截 ---


def test_gate_blocks_generating(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)
        from app.models import Job, ids

        j = Job(
            id=ids.job_id(),
            project_id=pid,
            job_type="generate_keyframe",
            status="queued",
            params={"__character_id__": entities["character"]},
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(j)
        db.commit()

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-gen")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_generating"


# --- 场景 6/7：待审核 / 审核驳回 → 拦截 ---


def test_gate_blocks_pending_review(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-pr")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_review_pending"


def test_gate_blocks_rejected_review(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    r = client.post(f"/api/projects/{pid}/assets-review", json={"action": "reject"})
    assert r.status_code == 200
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-rj")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_review_rejected"


# --- 场景 8：资产版本失效 → 拦截 ---


def test_gate_blocks_invalid_version(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)
        from app.models import Asset, Character

        ch = db.get(Character, entities["character"])
        a = db.get(Asset, ch.portrait_asset_id)
        a.validity = "superseded"
        db.commit()

    _with_db(_fill)
    # 审核接口同样拦截失效版本
    r = _approve_review(client, pid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-iv")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    assert any(b["reason"] == "asset_invalid" for b in r.json()["detail"].get("blockers", []))


# --- 场景 9：审核通过后重新生成 → 审核回退，重新拦截 ---


def test_regenerate_resets_review_and_blocks(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    r = _approve_review(client, pid)
    assert r.status_code == 200, r.text
    # 触发角色定妆图重新生成（API 通道），审核状态应回退 pending
    wf = _make_workflow(client, "gate-rg")
    r = client.post(f"/api/characters/{entities['character']}/generate", json={"workflow_id": wf})
    assert r.status_code == 202, r.text

    def _check(db):
        from app.models import Project

        p = db.get(Project, pid)
        return p.assets_review_status or "pending"

    assert _with_db(_check) == "pending"
    sid = _make_shot(client, entities["scene"])
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] in ("assets_generating", "assets_review_pending")


# --- 场景 10：批量生成不能绕过门禁 ---


def test_batch_generate_blocked(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    # 未审核 → 项目批量 keyframe 通道拦截
    r = client.post(f"/api/projects/{pid}/generate", json={"target": "keyframe", "shot_ids": [sid]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_review_pending"


# --- 场景 11：Agent 通道不能绕过门禁 ---


def test_agent_channel_blocked(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])

    def _seed(db):
        import hashlib

        from app.models import AgentConfirmation, AgentTask, Job, Project, ScriptVersion

        # 状态机门禁前置状态：剧本版本 + 评估哈希一致 + parse 任务已成功，
        # 使 derive_progress 推进到 keyframes 阶段（本用例验证 D-23 资产门禁）
        project = db.get(Project, pid)
        script_text = "第一场：主角在河边。"
        script_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
        script_version = ScriptVersion(
            id="scv_test_agent_0001",
            project_id=pid,
            version=1,
            content_snapshot=script_text,
            content_hash=script_hash,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(script_version)
        db.flush()
        project.current_script_version_id = script_version.id
        project.estimated_text_hash = script_hash
        db.add(
            Job(
                id="job_test_parse_0001",
                project_id=pid,
                job_type="parse",
                status="succeeded",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        task = AgentTask(
            id="agt_test_0001",
            session_id="ags_test_0001",
            intent="generate",
            intent_goal="生成分镜图",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(task)
        conf = AgentConfirmation(
            id="agc_test_0001",
            task_id=task.id,
            tool_name="run_generate",
            args={"project_id": pid, "shot_id": sid, "target": "keyframe"},
            status="approved",
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(conf)
        db.commit()

    _with_db(_seed)
    from app.services.agent_tools import ToolError, tool_run_generate

    def _run(db):
        with pytest.raises(ToolError) as exc_info:
            tool_run_generate(db, "agc_test_0001")
        return exc_info.value.code

    assert _with_db(_run) == "assets_review_pending"


# --- 场景 12：执行器二次校验（入队后资产状态变化） ---


def test_executor_rechecks_gate(client: TestClient, tmp_path):
    """任务入队后审核状态回退 → 执行器二次校验拦截，任务标记 failed。"""
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    r = _approve_review(client, pid)
    assert r.status_code == 200, r.text
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-ex")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 202, r.text
    jid = r.json().get("id") or r.json().get("job", {}).get("id")

    def _reset(db):
        from app.models import Project

        p = db.get(Project, pid)
        p.assets_review_status = "pending"
        db.commit()

    _with_db(_reset)
    from app.worker.executor import execute_generate_job

    result = execute_generate_job(jid)
    assert result["ok"] is False
    assert result["reason"].startswith("assets_gate:")

    def _verify(db):
        from app.models import Job

        job = db.get(Job, jid)
        assert job.status == "failed"
        assert job.error_code == "assets_review_pending"
        snap = job.input_snapshot or {}
        assert "assets_gate" in snap

    _with_db(_verify)
