"""候选版本与幂等提交测试（多图参考重新生成工作台 V1）。

覆盖：
- 相同幂等键重复提交不产生重复任务
- 采用候选后目标版本转生效、原生效版本转历史
- 失败/重复提交不改变当前生效版本指针
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.database import make_engine
from app.models import Asset, Base, Job, Workflow
from app.services import regeneration as regen

BACKEND_DIR = Path(__file__).resolve().parents[1]
H3_CAPS = json.loads(
    (BACKEND_DIR / "data" / "workflows" / "wf_video_h3_ref2va.meta.json").read_text("utf-8")
)["capabilities"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


def _file(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"img")
    return str(p)


def _setup(client, tmp_path):
    import app.core.database as db_mod

    pid = client.post("/api/projects", json={"name": "候选测试", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "草地", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()

    db = db_mod.SessionLocal()
    try:
        wf = Workflow(
            id="wf_h3", name="wf_video_h3_ref2va", workflow_type="video",
            definition={"1": {"class_type": "X", "inputs": {}, "_meta": {"title": "$output.clip"}}},
            capabilities=H3_CAPS, version=3, is_builtin=True,
        )
        db.add(wf)
        db.flush()

        src = Job(
            id="job_src_0001", project_id=pid, shot_id=shot["id"], job_type="generate_clip",
            workflow_id=wf.id, workflow_version=3,
            params={"prompt": "原始提示词", "width": 736, "height": 1312, "length": 192},
            status="succeeded", created_at=regen._now(),
            input_snapshot={"workflow_version": 3, "params": {"prompt": "原始提示词"}},
        )
        db.add(src)
        db.flush()

        db.add(Asset(
            id="ast_cur_001", project_id=pid, asset_type="clip", name="当前版本",
            version=1, file_path=_file(tmp_path, "cur.mp4"),
            source_job_id=src.id, validity="current",
        ))
        db.add(Asset(
            id="ast_cand_002", project_id=pid, asset_type="clip", name="候选版本",
            version=2, file_path=_file(tmp_path, "cand.mp4"),
            source_job_id=src.id, validity="candidate",
        ))
        # 满足既有静图先行闸口：视频生成要求分镜图已确认，这是产品规则不是被测逻辑
        from app.models import Shot
        _s = db.get(Shot, shot["id"])
        if _s is not None:
            _s.keyframe_confirmed = True
        db.commit()
    finally:
        db.close()

    return {"project_id": pid, "shot_id": shot["id"]}


def test_idempotent_submit_creates_single_job(client, tmp_path):
    """相同幂等键重复提交，只产生一个任务。"""
    ctx = _setup(client, tmp_path)
    body = {
        "target": "clip",
        "mode": "full_reconfigure",
        "prompt": "新提示词",
        "idempotency_key": "fixed-key-001",
    }
    r1 = client.post(f"/api/shots/{ctx['shot_id']}/regenerate/workbench", json=body)
    assert r1.status_code == 202, r1.text
    r2 = client.post(f"/api/shots/{ctx['shot_id']}/regenerate/workbench", json=body)
    assert r2.status_code == 202, r2.text
    assert r1.json()["id"] == r2.json()["id"]

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        count = db.query(Job).filter(Job.request_key == "fixed-key-001").count()
        assert count == 1
    finally:
        db.close()


def test_candidates_endpoint_lists_only_candidates(client, tmp_path):
    """候选列表只返回候选版本，不包含当前生效版本。"""
    ctx = _setup(client, tmp_path)
    r = client.get(f"/api/assets/ast_cur_001/candidates")
    assert r.status_code == 200, r.text
    ids = [c["asset_id"] for c in r.json()["candidates"]]
    assert "ast_cand_002" in ids
    assert "ast_cur_001" not in ids


def test_adopt_candidate_switches_current(client, tmp_path):
    """采用候选后：候选转生效，原生效版本转历史。"""
    ctx = _setup(client, tmp_path)
    r = client.post("/api/assets/ast_cand_002/adopt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["adopted_asset_id"] == "ast_cand_002"
    assert body["validity"] == "current"

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        assert db.get(Asset, "ast_cand_002").validity == "current"
        assert db.get(Asset, "ast_cur_001").validity == "superseded"
    finally:
        db.close()


def test_adopt_rejects_non_candidate(client, tmp_path):
    """非候选版本不可被采用，返回明确错误。"""
    _setup(client, tmp_path)
    r = client.post("/api/assets/ast_cur_001/adopt")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "invalid_state_transition"


def test_failed_submit_keeps_current_untouched(client, tmp_path):
    """提交被拒（如必需槽位缺失）时，当前生效版本不受影响。"""
    ctx = _setup(client, tmp_path)
    caps = json.loads(json.dumps(H3_CAPS))
    caps["reference_slots"][0]["optional"] = False

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        wf = db.get(Workflow, "wf_h3")
        wf.capabilities = caps
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/shots/{ctx['shot_id']}/regenerate/workbench", json={
        "target": "clip", "mode": "modify_refs", "prompt": "x", "refs": [],
    })
    assert r.status_code == 422, r.text

    db = db_mod.SessionLocal()
    try:
        assert db.get(Asset, "ast_cur_001").validity == "current"
        assert db.get(Asset, "ast_cand_002").validity == "candidate"
    finally:
        db.close()


def test_adopt_entity_candidate_leaves_single_current(client, tmp_path):
    """实体类候选（无镜头归属）采用后，不得出现两个同时生效的版本。"""
    ctx = _setup(client, tmp_path)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        db.add(Asset(
            id="ast_portrait_cur", project_id=ctx["project_id"], asset_type="character",
            name="主角定妆图", version=1, file_path=_file(tmp_path, "p1.png"), validity="current",
        ))
        db.add(Asset(
            id="ast_portrait_cand", project_id=ctx["project_id"], asset_type="character",
            name="主角定妆图", version=2, file_path=_file(tmp_path, "p2.png"), validity="candidate",
        ))
        db.add(Asset(
            id="ast_other_cur", project_id=ctx["project_id"], asset_type="character",
            name="配角定妆图", version=1, file_path=_file(tmp_path, "q1.png"), validity="current",
        ))
        db.commit()
    finally:
        db.close()

    r = client.post("/api/assets/ast_portrait_cand/adopt")
    assert r.status_code == 200, r.text
    assert "ast_portrait_cur" in r.json()["superseded"]

    db = db_mod.SessionLocal()
    try:
        cur = db.query(Asset).filter(
            Asset.project_id == ctx["project_id"],
            Asset.asset_type == "character",
            Asset.name == "主角定妆图",
            Asset.validity == "current",
        ).all()
        assert len(cur) == 1, f"同一实体资产只能有一个生效版本，实际 {len(cur)} 个"
        assert cur[0].id == "ast_portrait_cand"
        assert db.get(Asset, "ast_portrait_cur").validity == "superseded"
        assert db.get(Asset, "ast_other_cur").validity == "current"
    finally:
        db.close()
