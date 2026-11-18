"""镜头链尾帧依赖与失效传播测试（P0 整改）。

覆盖：
- shot_chain.resolve_prev_frame：有/无尾帧时的行为
- shot_chain.invalidate_chain_after_regenerate：clip vs keyframe 重做的差异
- 同集非首镜 clip 生成：ref_prev_frame 注入与 input_snapshot 记录
- requires_prev_frame=True 但无可用尾帧 → 409 prev_frame_missing
- 集首镜跳过尾帧注入（交跨集连续性处理）
- 批量 clip 预检：声明需要但不可用时统一 409，不部分入队
- regenerate_shot clip 路径：失效传播与 input_snapshot 记录
- prev_frame 判定始终写入 input_snapshot（无论成功/失败/跳过）
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Asset, Base, Job, Shot

import app.core.database as db_mod


# ── fixtures ──────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离的 TestClient，使用临时 SQLite 并绕过运行时配置校验。"""
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    import app.api.v1.projects as projects_mod
    monkeypatch.setattr(projects_mod, "_check_runtime_config", lambda: None)
    return TestClient(app)


def _session():
    """返回与测试引擎绑定的独立 SQLAlchemy Session。"""
    return db_mod.SessionLocal()


# ── 构造助手 ──────────────────────────────────────────────────────


def _make_project(client, aspect_ratio="9:16"):
    r = client.post("/api/projects", json={"name": "链测试", "aspect_ratio": aspect_ratio})
    assert 200 <= r.status_code < 300, r.text
    return r.json()["id"]


def _make_two_shots(client, pid, episode=1):
    """同场景同集两个镜头，返回 (scene, shot1, shot2)。"""
    scn = client.post("/api/scenes", json={
        "project_id": pid, "title": "测试场", "episode": episode,
    }).json()
    s1 = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": episode,
        "shot_kind": "visual", "duration": 4,
    }).json()
    s2 = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": episode,
        "shot_kind": "visual", "duration": 4,
    }).json()
    return scn, s1, s2


def _make_three_shots(client, pid, episode=1):
    """同场景同集三个镜头，返回 (scene, [shot1, shot2, shot3])。"""
    scn = client.post("/api/scenes", json={
        "project_id": pid, "title": "多镜场", "episode": episode,
    }).json()
    shots = []
    for _ in range(3):
        sh = client.post("/api/shots", json={
            "scene_id": scn["id"], "episode": episode,
            "shot_kind": "visual", "duration": 4,
        }).json()
        shots.append(sh)
    return scn, shots


def _make_video_workflow(client, name="v-wf"):
    r = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": "video"},
        files={"file": (
            f"{name}.json",
            # P1 起上传会做静态校验：工作流必须带 $output.* 标记（WORKFLOW.md §2 P0），
            # 故测试夹具补一个 $output.clip，而不是放宽校验
            json.dumps({"1": {"class_type": "EmptyLatentImage",
                               "_meta": {"title": "$output.clip"},
                               "inputs": {"width": 512, "height": 288, "batch_size": 1}}}),
            "application/json",
        )},
    )
    assert 200 <= r.status_code < 300, r.text
    return r.json()


# ── shot_chain 单元测试 ───────────────────────────────────────────


def test_resolve_prev_frame_returns_dict_when_asset_exists(client, tmp_path):
    """前一镜有有效 last_frame 资产且文件存在时返回完整字段字典。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    frame_file = tmp_path / "last.png"
    frame_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    s = _session()
    try:
        asset = Asset(
            id="ta-resolve", project_id=pid, asset_type="last_frame",
            name="test-lf", version=1, file_path=str(frame_file), validity="current",
        )
        s.add(asset)
        s.get(Shot, s1["id"]).last_frame_asset_id = "ta-resolve"
        s.commit()

        from app.services.shot_chain import resolve_prev_frame
        shot2 = s.get(Shot, s2["id"])
        result = resolve_prev_frame(s, shot2)
        assert result is not None
        assert result["file_path"] == str(frame_file)
        assert result["prev_shot_id"] == s1["id"]
    finally:
        s.close()


def test_resolve_prev_frame_returns_none_when_no_asset(client):
    """上一镜没有 last_frame 资产时返回 None。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    s = _session()
    try:
        from app.services.shot_chain import resolve_prev_frame
        shot2 = s.get(Shot, s2["id"])
        assert resolve_prev_frame(s, shot2) is None
    finally:
        s.close()


def test_previous_shot_returns_none_for_first_shot(client):
    """同集第一个镜头的 previous_shot 返回 None。"""
    pid = _make_project(client)
    _, s1, _ = _make_two_shots(client, pid)

    s = _session()
    try:
        from app.services.shot_chain import previous_shot
        shot1 = s.get(Shot, s1["id"])
        assert previous_shot(s, shot1) is None
    finally:
        s.close()


def test_previous_shot_returns_prev_for_second_shot(client):
    """同集第二个镜头的 previous_shot 返回第一个镜头。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    s = _session()
    try:
        from app.services.shot_chain import previous_shot
        shot2 = s.get(Shot, s2["id"])
        prev = previous_shot(s, shot2)
        assert prev is not None
        assert prev.id == s1["id"]
    finally:
        s.close()


def test_invalidate_clip_retains_keyframe_confirmed(client, tmp_path):
    """clip 重做清旧尾帧但保留 keyframe_confirmed，不下游扩散误清确认状态。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    frame_file = tmp_path / "f-clip.png"
    frame_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    s = _session()
    try:
        asset = Asset(
            id="ta-clip", project_id=pid, asset_type="last_frame",
            name="f", version=1, file_path=str(frame_file), validity="current",
        )
        s.add(asset)
        shot1 = s.get(Shot, s1["id"])
        shot1.last_frame_asset_id = "ta-clip"
        shot1.keyframe_confirmed = True
        s.commit()

        from app.services.shot_chain import invalidate_chain_after_regenerate
        info = invalidate_chain_after_regenerate(s, shot1, reset_keyframe_confirmed=False)
        assert info["cleared_last_frame"] is True
        assert info["keyframe_confirmed_reset"] is False
        assert s2["id"] in info["downstream_shot_ids"]
        s.refresh(shot1)
        assert shot1.keyframe_confirmed is True
        assert s.get(Asset, "ta-clip").validity == "superseded"
    finally:
        s.close()


def test_invalidate_keyframe_resets_confirmed(client, tmp_path):
    """keyframe 重做同时清尾帧和重置确认状态。"""
    pid = _make_project(client)
    _, s1, _ = _make_two_shots(client, pid)

    frame_file = tmp_path / "f-kf.png"
    frame_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    s = _session()
    try:
        asset = Asset(
            id="ta-kf", project_id=pid, asset_type="last_frame",
            name="f", version=1, file_path=str(frame_file), validity="current",
        )
        s.add(asset)
        shot1 = s.get(Shot, s1["id"])
        shot1.last_frame_asset_id = "ta-kf"
        shot1.keyframe_confirmed = True
        s.commit()

        from app.services.shot_chain import invalidate_chain_after_regenerate
        info = invalidate_chain_after_regenerate(s, shot1, reset_keyframe_confirmed=True)
        assert info["cleared_last_frame"] is True
        assert info["keyframe_confirmed_reset"] is True
        s.flush()  # 确保 pending 修改写入 DB 后再 reload（autoflush=False）
        s.refresh(shot1)
        assert shot1.keyframe_confirmed is False
        assert s.get(Asset, "ta-kf").validity == "superseded"
    finally:
        s.close()


# ── API 注入集成测试 ──────────────────────────────────────────────


def test_clip_injects_ref_prev_frame_for_non_first_shot(client, tmp_path):
    """同集非首镜生成 clip 时注入 ref_prev_frame 到 __media__ 并持久化判定。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    frame_file = tmp_path / "inject.png"
    frame_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    s = _session()
    try:
        asset = Asset(
            id="ta-inj", project_id=pid, asset_type="last_frame",
            name="f-inj", version=1, file_path=str(frame_file), validity="current",
        )
        s.add(asset)
        s.get(Shot, s1["id"]).last_frame_asset_id = "ta-inj"
        s.get(Shot, s2["id"]).keyframe_confirmed = True
        s.commit()
    finally:
        s.close()

    wf = _make_video_workflow(client, "inj-wf")
    r = client.post(
        f"/api/shots/{s2['id']}/generate",
        json={"target": "clip", "workflow_id": wf["id"]},
    )
    assert r.status_code == 202, r.text
    eff = r.json().get("effective_inputs", {})
    assert eff.get("prev_frame", {}).get("injected") is True
    assert eff.get("prev_frame", {}).get("prev_shot_id") == s1["id"]


def test_requires_prev_frame_missing_returns_409(client, tmp_path):
    """requires_prev_frame=True 但上一镜无可用尾帧时返回 409，不静默绕过。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    s = _session()
    try:
        s.get(Shot, s2["id"]).requires_prev_frame = True
        s.get(Shot, s2["id"]).keyframe_confirmed = True
        s.commit()
    finally:
        s.close()

    wf = _make_video_workflow(client, "req-wf")
    r = client.post(
        f"/api/shots/{s2['id']}/generate",
        json={"target": "clip", "workflow_id": wf["id"]},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "prev_frame_missing"


def test_first_shot_skips_prev_frame(client):
    """集首镜生成 clip 不注入尾帧，reason 标记为连续性流程接管。"""
    pid = _make_project(client)
    scn = client.post("/api/scenes", json={
        "project_id": pid, "title": "首镜场", "episode": 1,
    }).json()
    s1 = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1,
        "shot_kind": "visual", "duration": 4,
    }).json()

    s = _session()
    try:
        s.get(Shot, s1["id"]).keyframe_confirmed = True
        s.commit()
    finally:
        s.close()

    wf = _make_video_workflow(client, "first-wf")
    r = client.post(
        f"/api/shots/{s1['id']}/generate",
        json={"target": "clip", "workflow_id": wf["id"]},
    )
    assert r.status_code == 202, r.text
    eff = r.json().get("effective_inputs", {})
    assert eff.get("prev_frame", {}).get("reason") == "first_shot_handled_by_continuity"


def test_batch_clip_precheck_blocks_all_or_none(client, tmp_path):
    """批量 clip 预检：有镜头声明需要尾帧但不可用时统一 409，不出现部分入队。"""
    pid = _make_project(client)
    scn, shots = _make_three_shots(client, pid)

    s = _session()
    try:
        s.get(Shot, shots[1]["id"]).requires_prev_frame = True
        for sh in shots:
            s.get(Shot, sh["id"]).keyframe_confirmed = True
        s.commit()
    finally:
        s.close()

    wf = _make_video_workflow(client, "batch-wf")
    r = client.post(
        f"/api/scenes/{scn['id']}/generate",
        json={"target": "clip", "workflow_id": wf["id"]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "prev_frame_missing"

    # 确认没有任务被创建（全量拦截，无部分入队）
    s2 = _session()
    try:
        assert s2.query(Job).count() == 0
    finally:
        s2.close()


def test_regenerate_clip_persists_chain_invalidation(client, tmp_path):
    """clip 重做在 input_snapshot 记录失效传播（下游清单、确认状态不变）。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    frame_file = tmp_path / "regen.png"
    frame_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    s = _session()
    try:
        asset = Asset(
            id="ta-regen", project_id=pid, asset_type="last_frame",
            name="f-rg", version=1, file_path=str(frame_file), validity="current",
        )
        s.add(asset)
        shot1 = s.get(Shot, s1["id"])
        shot1.last_frame_asset_id = "ta-regen"
        shot1.keyframe_confirmed = True
        s.commit()
    finally:
        s.close()

    wf = _make_video_workflow(client, "regen-wf")
    r = client.post(
        f"/api/shots/{s1['id']}/regenerate",
        json={"target": "clip", "workflow_id": wf["id"]},
    )
    assert r.status_code == 202, r.text
    eff = r.json().get("effective_inputs", {})
    ci = eff.get("chain_invalidation", {})
    assert ci.get("cleared_last_frame") is True
    assert ci.get("keyframe_confirmed_reset") is False
    assert s2["id"] in ci.get("downstream_shot_ids", [])


def test_prev_frame_snapshot_always_recorded_for_clip(client):
    """clip 生成无论注入成功与否都在 input_snapshot 记录 prev_frame 判定。"""
    pid = _make_project(client)
    _, s1, s2 = _make_two_shots(client, pid)

    s = _session()
    try:
        s.get(Shot, s2["id"]).keyframe_confirmed = True
        s.commit()
    finally:
        s.close()

    wf = _make_video_workflow(client, "snap-wf")
    r = client.post(
        f"/api/shots/{s2['id']}/generate",
        json={"target": "clip", "workflow_id": wf["id"]},
    )
    assert r.status_code == 202, r.text
    eff = r.json().get("effective_inputs", {})
    # 上一镜无尾帧 + 本镜未声明 requires_prev_frame → 记录原因但不报错
    pf = eff.get("prev_frame", {})
    assert "injected" in pf or "reason" in pf
