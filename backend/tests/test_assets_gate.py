"""统一资产就绪门禁测试（2026-11 流程简化后）。

核心规则：所有必需角色、场景、道具的图片均已真实生成完成、文件有效，
且没有正在生成的必需资产，才允许开始分镜图生成。

**人工审核闸口已取消**：不再有 assets-review 接口、不再有
assets_review_pending / assets_review_rejected 阻断原因。

覆盖场景（任务书 §10.1）：
1. 全部生成完成 → 放行（可打开确认弹窗）
2. 缺少必需角色 → 拦截
3. 缺少必需场景 → 拦截
4. 缺少必需道具 → 拦截
5. 缺少可选道具（anchor_required=False）→ 不拦截
6. 必需资产仍在生成 → 拦截（assets_generating）
7. 资产版本失效 → 拦截
8. 资产文件不存在 → 拦截
9. 批量通道不能绕过门禁
10. Agent 通道不能绕过门禁
11. 执行器二次校验（入队后资产被删除/失效）
12. 门禁返回分项统计 summary（角色/场景/道具 total·done）
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


def _add_entities(client: TestClient, project_id: str, *, optional_prop: bool = False) -> dict:
    """创建角色/场景/道具实体（镜头挂在已建场景下，避免产生第二个无基准图的场景）。"""
    from app.core.database import SessionLocal
    from app.models import Character, Prop, Scene, SceneEpisode

    db = SessionLocal()
    try:
        ch = Character(id="chr_test_0001", project_id=project_id, name="主角")
        sc = Scene(id="scn_test_0001", project_id=project_id, seq=1, title="教室")
        db.add_all([ch, sc])
        db.add(SceneEpisode(scene_id=sc.id, episode=1, seq_in_episode=1))
        pr = Prop(id="prp_test_0001", project_id=project_id, name="宝剑", anchor_required=True)
        db.add(pr)
        out = {"character": ch.id, "scene": sc.id, "prop": pr.id}
        if optional_prop:
            op = Prop(
                id="prp_test_0002",
                project_id=project_id,
                name="背景报纸",
                anchor_required=False,
            )
            db.add(op)
            out["optional_prop"] = op.id
        db.commit()
        return out
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


def _make_asset(db, project_id: str, asset_type: str, name: str, file_path: str,
                validity: str = "current") -> str:
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


def _generate_keyframe(client: TestClient, shot_id: str, workflow_id: str):
    return client.post(
        f"/api/shots/{shot_id}/generate",
        json={"target": "keyframe", "workflow_id": workflow_id},
    )


# --- 场景 1：全部生成完成 → 放行 ---


def test_gate_passes_when_all_ready(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-pass")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 202, r.text


def test_gate_summary_reports_per_type_progress(client: TestClient, tmp_path):
    """门禁结果携带分项统计：角色/场景/道具各自的 total·done，供前端展示进度。"""
    pid = _make_project(client)
    entities = _add_entities(client, pid, optional_prop=True)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities, skip={"scene"})

    _with_db(_fill)
    from app.services.assets_gate import evaluate_assets_gate

    def _gate(db):
        return evaluate_assets_gate(db, pid).to_dict()

    data = _with_db(_gate)
    assert data["ready"] is False
    assert data["summary"]["character"] == {"total": 1, "done": 1}
    assert data["summary"]["scene"] == {"total": 1, "done": 0}
    # 可选道具不计入必需统计（anchor_required=False）
    assert data["summary"]["prop"] == {"total": 1, "done": 1}
    assert data["total"] == 3 and data["done"] == 2
    assert [b["name"] for b in data["blockers"]] == ["教室"]


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
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, f"gate-m-{expect_type}")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    blockers = r.json()["detail"].get("blockers", [])
    assert any(b["asset_type"] == expect_type and b["reason"] == "asset_missing" for b in blockers)


# --- 场景 5：可选道具缺失不阻断 ---


def test_gate_ignores_optional_prop(client: TestClient, tmp_path):
    """anchor_required=False 的道具没有图片时，不得阻断进入分镜图。"""
    pid = _make_project(client)
    entities = _add_entities(client, pid, optional_prop=True)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    from app.services.assets_gate import evaluate_assets_gate

    def _gate(db):
        return evaluate_assets_gate(db, pid)

    result = _with_db(_gate)
    assert result.ready is True
    # 可选道具不进入统计分母
    assert result.summary["prop"]["total"] == 1


# --- 场景 6：必需资产仍在生成 → 拦截 ---


def test_gate_blocks_generating(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities, skip={"character"})
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


# --- 场景 7：资产版本失效 → 拦截 ---


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
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-iv")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    assert any(b["reason"] == "asset_invalid" for b in r.json()["detail"].get("blockers", []))


# --- 场景 8：资产文件不存在 → 拦截 ---


def test_gate_blocks_missing_file(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)
        from app.models import Asset, Scene

        sc = db.get(Scene, entities["scene"])
        a = db.get(Asset, sc.baseline_asset_id)
        a.file_path = str(tmp_path / "media" / "gone.png")  # 记录存在但文件不存在
        db.commit()

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-mf")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"
    assert any(b["reason"] == "asset_file_missing" for b in r.json()["detail"].get("blockers", []))


# --- 场景 9：批量生成不能绕过门禁 ---


def test_batch_generate_blocked(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities, skip={"prop"})

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    r = client.post(f"/api/projects/{pid}/generate", json={"target": "keyframe", "shot_ids": [sid]})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "assets_incomplete"


# --- 场景 10：Agent 通道不能绕过门禁 ---


def test_agent_channel_blocked(client: TestClient, tmp_path):
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities, skip={"prop"})

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])

    def _seed(db):
        import hashlib

        from app.models import AgentConfirmation, AgentTask, Job, Project, ScriptVersion

        # 状态机门禁前置状态：剧本版本 + 评估哈希一致 + parse 任务已成功，
        # 使 derive_progress 推进到 keyframes 阶段（本用例验证资产门禁）
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

    assert _with_db(_run) == "assets_incomplete"


# --- 场景 11：执行器二次校验（入队后资产状态变化） ---


def test_executor_rechecks_gate(client: TestClient, tmp_path):
    """任务入队后必需资产文件被删除 → 执行器二次校验拦截，任务标记 failed。"""
    pid = _make_project(client)
    entities = _add_entities(client, pid)

    def _fill(db):
        _bind_assets(db, pid, tmp_path, entities)

    _with_db(_fill)
    sid = _make_shot(client, entities["scene"])
    wf = _make_workflow(client, "gate-ex")
    r = _generate_keyframe(client, sid, wf)
    assert r.status_code == 202, r.text
    jid = r.json().get("id") or r.json().get("job", {}).get("id")

    def _break(db):
        """入队后把场景基准图转 superseded：模拟资产在队列期间被替换/失效。"""
        from app.models import Asset, Scene

        sc = db.get(Scene, entities["scene"])
        a = db.get(Asset, sc.baseline_asset_id)
        a.validity = "superseded"
        db.commit()

    _with_db(_break)
    from app.worker.executor import execute_generate_job

    result = execute_generate_job(jid)
    assert result["ok"] is False
    assert result["reason"].startswith("assets_gate:")

    def _verify(db):
        from app.models import Job

        job = db.get(Job, jid)
        assert job.status == "failed"
        assert job.error_code == "assets_incomplete"
        snap = job.input_snapshot or {}
        assert "assets_gate" in snap

    _with_db(_verify)


# --- 审核机制确实已移除 ---


def test_assets_review_endpoint_removed(client: TestClient):
    """资产人工审核接口已删除：不再有审核通过/驳回入口，也不会写审核状态。"""
    pid = _make_project(client)
    r = client.post(f"/api/projects/{pid}/assets-review", json={"action": "approve"})
    assert r.status_code in (404, 405), r.text
