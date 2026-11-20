"""阶段就绪检查测试（2026-11 流程简化）。

覆盖任务书 §10.2 / §10.3 的分镜图与分镜视频跳转判定：

分镜图阶段（storyboard_images）：
1. 所有分镜图片生成完成 → ready，允许确认进入分镜看板
2. 缺少一张分镜图 → 阻止跳转，missing 里指出该镜
3. 分镜图正在生成且无有效版本 → 阻止跳转
4. 分镜图生成失败且无有效旧图 → 阻止跳转
5. 分镜图片文件不存在 → 阻止跳转
6. 存在失效图片（superseded 且无 current）→ 阻止跳转
7. 项目无镜头 → 阻止跳转

分镜视频阶段（storyboard_videos）：
8. 所有镜头视频生成完成 → ready
9. 缺少一个镜头视频 → 阻止跳转
10. 视频正在生成且无有效旧视频 → 阻止跳转
11. 视频文件不存在 → 阻止跳转
12. 存在有效旧视频 + 正在重新生成 → 按有效版本规则放行
13. 多集项目有一集缺少视频 → 阻止整个项目跳转

接口层：
14. GET /api/projects/{id}/stage-readiness/{stage} 返回统一结构
15. 非法 stage → 400
"""
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
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _project(client: TestClient, name: str = "就绪项目") -> str:
    return client.post("/api/projects", json={"name": name, "aspect_ratio": "9:16"}).json()["id"]


def _scene(client: TestClient, pid: str, title: str = "教室", seq: int = 1) -> dict:
    return client.post("/api/scenes", json={"project_id": pid, "title": title, "episode": 1, "seq": seq}).json()


def _ensure_scene_episode(scene_id: str, episode: int, seq_in_episode: int = 1) -> None:
    """镜头创建要求 (scene_id, episode) 存在出场记录，多集用例需显式补第 N 集。"""
    from app.models import SceneEpisode

    def _add(db):
        exists = (
            db.query(SceneEpisode)
            .filter(SceneEpisode.scene_id == scene_id, SceneEpisode.episode == episode)
            .count()
        )
        if not exists:
            db.add(SceneEpisode(scene_id=scene_id, episode=episode, seq_in_episode=seq_in_episode))
            db.commit()

    _with_db(_add)


def _shots(client: TestClient, scene_id: str, count: int, episode: int = 1) -> list[str]:
    _ensure_scene_episode(scene_id, episode)
    ids = []
    for _ in range(count):
        s = client.post("/api/shots", json={
            "scene_id": scene_id, "episode": episode, "shot_kind": "visual", "duration": 5,
        }).json()
        ids.append(s["id"])
    return ids


def _with_db(fn):
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def _make_shot_asset(
    db,
    *,
    project_id: str,
    shot_id: str,
    asset_type: str,
    tmp_path,
    validity: str = "current",
    job_status: str = "succeeded",
    file_exists: bool = True,
) -> str:
    """构造「镜头 + 产物 + 来源任务」完整链路（就绪判定依赖 source_job_id → job）。"""
    from app.models import Asset, Job, ids

    job = Job(
        id=ids.job_id(),
        project_id=project_id,
        shot_id=shot_id,
        job_type="generate_keyframe" if asset_type == "keyframe" else "generate_clip",
        status=job_status,
        created_at=_now(),
        finished_at=_now() if job_status in ("succeeded", "failed") else None,
    )
    db.add(job)
    db.flush()

    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    fname = f"{asset_type}_{ids.asset_id()}.png"
    fpath = media / fname
    if file_exists:
        fpath.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    a = Asset(
        id=ids.asset_id(),
        project_id=project_id,
        asset_type=asset_type,
        name=f"{shot_id}-{asset_type}",
        version=1,
        file_path=str(fpath),
        validity=validity,
        source_job_id=job.id,
    )
    db.add(a)
    db.commit()
    return a.id


# ---------------- 分镜图阶段 ----------------


def test_images_ready_when_all_generated(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 3)

    def _fill(db):
        for sid in shots:
            _make_shot_asset(db, project_id=pid, shot_id=sid, asset_type="keyframe", tmp_path=tmp_path)

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is True
    assert (r.total, r.done) == (3, 3)
    assert r.blockers == []


def test_images_blocked_when_one_missing(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 3)

    def _fill(db):
        for sid in shots[:2]:
            _make_shot_asset(db, project_id=pid, shot_id=sid, asset_type="keyframe", tmp_path=tmp_path)

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is False
    assert (r.total, r.done) == (3, 2)
    assert len(r.missing) == 1
    assert r.missing[0].shot_id == shots[2]
    assert r.missing[0].reason == "shot_missing_asset"
    assert r.missing[0].name == "第 1 集 第 3 镜"


def test_images_blocked_while_generating(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 2)

    def _fill(db):
        from app.models import Job, ids

        _make_shot_asset(db, project_id=pid, shot_id=shots[0], asset_type="keyframe", tmp_path=tmp_path)
        db.add(Job(
            id=ids.job_id(), project_id=pid, shot_id=shots[1],
            job_type="generate_keyframe", status="running", created_at=_now(),
        ))
        db.commit()

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is False
    assert r.generating == 1
    assert r.missing[0].reason == "shot_generating"


def test_images_blocked_on_failed_without_valid_version(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 2)

    def _fill(db):
        from app.models import Job, ids

        _make_shot_asset(db, project_id=pid, shot_id=shots[0], asset_type="keyframe", tmp_path=tmp_path)
        db.add(Job(
            id=ids.job_id(), project_id=pid, shot_id=shots[1],
            job_type="generate_keyframe", status="failed", created_at=_now(), finished_at=_now(),
        ))
        db.commit()

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is False
    assert r.failed == 1
    assert r.missing[0].reason == "shot_failed"


def test_images_blocked_when_file_missing(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 1)

    def _fill(db):
        _make_shot_asset(
            db, project_id=pid, shot_id=shots[0], asset_type="keyframe",
            tmp_path=tmp_path, file_exists=False,
        )

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is False
    assert r.done == 0
    assert r.missing[0].reason == "shot_file_missing"


def test_images_blocked_when_only_superseded_version(client, tmp_path):
    """只有失效版本（重绘后旧图）不算当前有效分镜图。"""
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 1)

    def _fill(db):
        _make_shot_asset(
            db, project_id=pid, shot_id=shots[0], asset_type="keyframe",
            tmp_path=tmp_path, validity="superseded",
        )

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is False
    assert r.missing[0].reason == "shot_missing_asset"


def test_images_blocked_without_shots(client):
    pid = _project(client)
    from app.services.stage_readiness import check_storyboard_images_ready

    r = _with_db(lambda db: check_storyboard_images_ready(db, pid))
    assert r.ready is False
    assert r.total == 0
    assert "还没有镜头" in r.message


# ---------------- 分镜视频阶段 ----------------


def test_videos_ready_when_all_generated(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 3)

    def _fill(db):
        for sid in shots:
            _make_shot_asset(db, project_id=pid, shot_id=sid, asset_type="clip", tmp_path=tmp_path)

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_videos_ready

    r = _with_db(lambda db: check_storyboard_videos_ready(db, pid))
    assert r.ready is True
    assert (r.total, r.done) == (3, 3)


def test_videos_blocked_when_one_missing(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 2)

    def _fill(db):
        _make_shot_asset(db, project_id=pid, shot_id=shots[0], asset_type="clip", tmp_path=tmp_path)

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_videos_ready

    r = _with_db(lambda db: check_storyboard_videos_ready(db, pid))
    assert r.ready is False
    assert r.missing[0].shot_id == shots[1]


def test_videos_blocked_while_regenerating_without_valid_old(client, tmp_path):
    """正在重新生成且旧版本已失效 → 必须等待新版本完成。"""
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 1)

    def _fill(db):
        from app.models import Job, ids

        _make_shot_asset(
            db, project_id=pid, shot_id=shots[0], asset_type="clip",
            tmp_path=tmp_path, validity="superseded",
        )
        db.add(Job(
            id=ids.job_id(), project_id=pid, shot_id=shots[0],
            job_type="generate_clip", status="running", created_at=_now(),
        ))
        db.commit()

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_videos_ready

    r = _with_db(lambda db: check_storyboard_videos_ready(db, pid))
    assert r.ready is False
    assert r.missing[0].reason == "shot_generating"


def test_videos_pass_with_valid_old_while_regenerating(client, tmp_path):
    """旧视频仍有效、同时正在重新生成 → 旧版本可作当前交付视频，不阻断跳转。"""
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 1)

    def _fill(db):
        from app.models import Job, ids

        _make_shot_asset(db, project_id=pid, shot_id=shots[0], asset_type="clip", tmp_path=tmp_path)
        db.add(Job(
            id=ids.job_id(), project_id=pid, shot_id=shots[0],
            job_type="generate_clip", status="running", created_at=_now(),
        ))
        db.commit()

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_videos_ready

    r = _with_db(lambda db: check_storyboard_videos_ready(db, pid))
    assert r.ready is True
    assert r.done == 1


def test_videos_blocked_when_file_missing(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 1)

    def _fill(db):
        _make_shot_asset(
            db, project_id=pid, shot_id=shots[0], asset_type="clip",
            tmp_path=tmp_path, file_exists=False,
        )

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_videos_ready

    r = _with_db(lambda db: check_storyboard_videos_ready(db, pid))
    assert r.ready is False
    assert r.missing[0].reason == "shot_file_missing"


def test_videos_blocked_when_one_episode_missing(client, tmp_path):
    """多集项目：只查第一集会把缺视频的另一集漏掉，必须阻断整体跳转。"""
    pid = _project(client)
    scn1 = _scene(client, pid, title="第1集场景", seq=1)
    scn2 = _scene(client, pid, title="第2集场景", seq=2)
    ep1_shots = _shots(client, scn1["id"], 2, episode=1)
    ep2_shots = _shots(client, scn2["id"], 2, episode=2)

    def _fill(db):
        for sid in ep1_shots:
            _make_shot_asset(db, project_id=pid, shot_id=sid, asset_type="clip", tmp_path=tmp_path)
        # 第 2 集只出了一镜
        _make_shot_asset(db, project_id=pid, shot_id=ep2_shots[0], asset_type="clip", tmp_path=tmp_path)

    _with_db(_fill)
    from app.services.stage_readiness import check_storyboard_videos_ready

    r = _with_db(lambda db: check_storyboard_videos_ready(db, pid))
    assert r.ready is False
    assert (r.total, r.done) == (4, 3)
    assert [b.episode for b in r.missing] == [2]
    assert r.missing[0].name == "第 2 集 第 2 镜"


# ---------------- HTTP 接口 ----------------


def test_stage_readiness_endpoint_shape(client, tmp_path):
    pid = _project(client)
    scn = _scene(client, pid)
    shots = _shots(client, scn["id"], 2)

    def _fill(db):
        for sid in shots:
            _make_shot_asset(db, project_id=pid, shot_id=sid, asset_type="keyframe", tmp_path=tmp_path)

    _with_db(_fill)
    body = client.get(f"/api/projects/{pid}/stage-readiness/storyboard_images").json()
    assert body["stage"] == "storyboard_images"
    assert body["ready"] is True
    assert body["total"] == 2 and body["done"] == 2
    for key in ("failed", "generating", "missing", "blockers"):
        assert key in body
    assert isinstance(body["missing"], list) and isinstance(body["blockers"], list)


def test_stage_readiness_invalid_stage(client):
    pid = _project(client)
    r = client.get(f"/api/projects/{pid}/stage-readiness/nonsense")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_request"


def test_stage_readiness_project_not_found(client):
    r = client.get("/api/projects/prj_notexist/stage-readiness/storyboard_images")
    assert r.status_code == 404


# ---------------- 资产库阶段（assets） ----------------
def _asset_entities(client: TestClient, project_id: str, *, optional_prop: bool = False) -> dict:
    """创建角色/场景/必需道具（场景走 API，便于后续挂镜头）。

    注意：经 API 建场景时后端已自动创建第 1 集出场记录，这里不能再插
    SceneEpisode，否则触发 (scene_id, episode) 唯一约束。
    """
    from app.core.database import SessionLocal
    from app.models import Character, Prop

    scn = _scene(client, project_id)
    db = SessionLocal()
    try:
        ch = Character(id="chr_stage_0001", project_id=project_id, name="主角")
        pr = Prop(
            id="prp_stage_0001",
            project_id=project_id,
            name="宝剑",
            anchor_required=True,
        )
        db.add_all([ch, pr])
        out = {"character": ch.id, "scene": scn["id"], "prop": pr.id}
        if optional_prop:
            op = Prop(
                id="prp_stage_0002",
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


def _bind_asset_ptr(
    db,
    project_id: str,
    kind: str,
    entity_id: str,
    tmp_path,
    *,
    validity: str = "current",
    write_file: bool = True,
) -> str:
    """给实体绑定一份资产并回填指针，文件真实写入磁盘（门禁会校验文件存在）。"""
    from app.models import Asset, Character, Prop, Scene, ids

    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    fpath = media / f"{kind}_{ids.asset_id()}.png"
    if write_file:
        fpath.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    a = Asset(
        id=ids.asset_id(),
        project_id=project_id,
        asset_type=kind,
        name=kind,
        version=1,
        file_path=str(fpath),
        validity=validity,
    )
    db.add(a)
    db.flush()
    if kind == "character":
        db.get(Character, entity_id).portrait_asset_id = a.id
    elif kind == "scene":
        db.get(Scene, entity_id).baseline_asset_id = a.id
    else:
        db.get(Prop, entity_id).anchor_asset_id = a.id
    db.commit()
    return a.id


def test_assets_stage_ready_when_all_required_generated(client, tmp_path):
    """必需角色/场景/必需道具图片全部生成 → 资产库阶段就绪。"""
    pid = _project(client)
    ents = _asset_entities(client, pid)

    def _fill(db):
        _bind_asset_ptr(db, pid, "character", ents["character"], tmp_path)
        _bind_asset_ptr(db, pid, "scene", ents["scene"], tmp_path)
        _bind_asset_ptr(db, pid, "prop", ents["prop"], tmp_path)

    _with_db(_fill)
    body = client.get(f"/api/projects/{pid}/stage-readiness/assets").json()
    assert body["stage"] == "assets"
    assert body["ready"] is True
    assert (body["total"], body["done"]) == (3, 3)
    assert body["blockers"] == []


def test_assets_stage_blocked_when_required_prop_missing(client, tmp_path):
    """缺必需道具 → 阻断，且阻断明细带资产类型（前端按类型展示）。"""
    pid = _project(client)
    ents = _asset_entities(client, pid)

    def _fill(db):
        _bind_asset_ptr(db, pid, "character", ents["character"], tmp_path)
        _bind_asset_ptr(db, pid, "scene", ents["scene"], tmp_path)

    _with_db(_fill)
    body = client.get(f"/api/projects/{pid}/stage-readiness/assets").json()
    assert body["ready"] is False
    assert (body["total"], body["done"]) == (3, 2)
    assert len(body["blockers"]) == 1
    b = body["blockers"][0]
    assert b["reason"] == "asset_missing"
    assert b["asset_type"] == "prop"
    assert b["asset_type_label"] == "道具"


def test_assets_stage_ignores_optional_prop(client, tmp_path):
    """可选道具（anchor_required=False）没图不阻断。"""
    pid = _project(client)
    ents = _asset_entities(client, pid, optional_prop=True)

    def _fill(db):
        _bind_asset_ptr(db, pid, "character", ents["character"], tmp_path)
        _bind_asset_ptr(db, pid, "scene", ents["scene"], tmp_path)
        _bind_asset_ptr(db, pid, "prop", ents["prop"], tmp_path)

    _with_db(_fill)
    body = client.get(f"/api/projects/{pid}/stage-readiness/assets").json()
    assert body["ready"] is True
    assert body["total"] == 3


def test_assets_stage_ready_even_when_keyframes_missing(client, tmp_path):
    """回归：资产库阶段不能误用分镜图阶段判定。

    资产齐全但分镜图尚未生成时，assets 阶段必须放行（否则用户永远进不了
    分镜图页面），而 storyboard_images 阶段仍应未就绪。
    """
    pid = _project(client)
    ents = _asset_entities(client, pid)

    def _fill(db):
        _bind_asset_ptr(db, pid, "character", ents["character"], tmp_path)
        _bind_asset_ptr(db, pid, "scene", ents["scene"], tmp_path)
        _bind_asset_ptr(db, pid, "prop", ents["prop"], tmp_path)

    _with_db(_fill)
    _shots(client, ents["scene"], 2)

    assets_body = client.get(f"/api/projects/{pid}/stage-readiness/assets").json()
    assert assets_body["ready"] is True

    images_body = client.get(
        f"/api/projects/{pid}/stage-readiness/storyboard_images"
    ).json()
    assert images_body["ready"] is False
    assert images_body["total"] == 2 and images_body["done"] == 0
