"""重新生成工作台验收测试。

覆盖：槽位绑定、多参考图、身份绑定、空间布局、动作时间轴、分层提示词、
连续性校验、参数快照、历史数据兼容。

设计约定：
- 槽位 node_id 一律来自真实工作流 definition 的 $slot 注入标记，不按下标猜测
- 音频槽位（ref_audio_*）不得占用图片槽位
- 连续性结论只做提示（auto_applied=False），不自动改写结构化配置
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.database import make_engine
from app.models import Asset, Base, Job, JobInputRef, Workflow
from app.models.ids import job_input_ref_id
from app.models.regen_config import StoryboardRegenConfig
from app.services import regeneration as regen
from app.services import regen_workspace as ws
from app.services.workflow.capabilities import normalize as normalize_capabilities

BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = BACKEND_DIR / "data" / "workflows"

H3_DEF = json.loads((WORKFLOW_DIR / "wf_video_h3_ref2va_std_5s.json").read_text("utf-8"))
# 自动解析能力（不再依赖 .meta.json）
H3_CAPS = normalize_capabilities("video", H3_DEF, capabilities=None, trigger=None)

IMAGE_SLOTS = [
    "ref_prev_frame", "ref_image", "ref_identity", "ref_character_2", "ref_prop",
    "ref_character_3", "ref_character_4", "ref_character_5", "ref_character_6",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine("sqlite:///{}".format(db_file))
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


def _media(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"fake-image-bytes")
    return str(p)


def _seed(client, tmp_path, two_shots=True):
    import app.core.database as db_mod
    resp = client.post("/api/projects", json={"name": "工作台验收", "aspect_ratio": "9:16"})
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "草地", "episode": 1}).json()
    shot1 = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    shot2 = None
    if two_shots:
        shot2 = client.post("/api/shots", json={
            "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
        }).json()

    db = db_mod.SessionLocal()
    try:
        wf = Workflow(
            id="wf_h3_real", name="wf_video_h3_ref2va_std_5s", workflow_type="video",
            definition=H3_DEF, capabilities=H3_CAPS, version=7, is_builtin=True,
        )
        db.add(wf)
        db.flush()

        shot_list = [shot1] + ([shot2] if shot2 else [])
        for idx, s in enumerate(shot_list, start=1):
            p = {"prompt": "原提示词", "width": 736, "height": 1312, "length": 192}
            job = Job(
                id="job_ws_{:03d}".format(idx), project_id=pid, shot_id=s["id"],
                job_type="generate_clip", workflow_id=wf.id, workflow_version=7,
                params=dict(p), status="succeeded", created_at=regen._now(),
                input_snapshot={"workflow_version": 7, "params": dict(p), "injected_media": {}},
            )
            db.add(job)
            db.flush()
            db.add(Asset(
                id="ast_clip_{:03d}".format(idx), project_id=pid, asset_type="clip",
                name="基础版本{}".format(idx), version=3,
                file_path=_media(tmp_path, "base{}.mp4".format(idx)),
                source_job_id=job.id, validity="current",
            ))

        ref_ids = []
        for idx in range(2):
            aid = "ast_ws_ref_{:03d}".format(idx)
            db.add(Asset(
                id=aid, project_id=pid, asset_type="character",
                name="参考角色{}".format(idx), version=2,
                file_path=_media(tmp_path, "ref{}.png".format(idx)), validity="current",
            ))
            ref_ids.append(aid)
        db.flush()
        for seq, aid in enumerate(ref_ids):
            slot = ["ref_identity", "ref_character_2"][seq]
            db.add(JobInputRef(
                id=job_input_ref_id(), job_id="job_ws_001",
                slot_param=slot, input_key="ref_images.ref_image_{}".format(seq + 2),
                asset_id=aid, asset_version=2, asset_type="character",
                role="character", required=False,
                file_path=_media(tmp_path, "ref{}.png".format(seq)),
                semantic="reference", enabled=True, seq=seq, created_at=regen._now(),
            ))
        db.commit()
    finally:
        db.close()

    return {
        "project_id": pid,
        "shot1": shot1["id"],
        "shot2": shot2["id"] if shot2 else None,
        "ref_ids": ref_ids,
    }


# ---------- 1. 槽位映射 ----------

def test_slot_node_map_uses_real_definition_and_excludes_audio():
    wf = SimpleNamespace(definition=H3_DEF, capabilities=H3_CAPS)
    nodes = ws.slot_node_map(wf)
    assert set(nodes) == set(IMAGE_SLOTS)
    assert len(nodes) == 9
    assert str(nodes["ref_prev_frame"]["node_id"]) == "30"
    assert str(nodes["ref_character_6"]["node_id"]) == "39"
    assert nodes["ref_prev_frame"]["image_index"] == 1
    assert nodes["ref_character_6"]["image_index"] == 9
    assert not any("audio" in name for name in nodes)


def test_slot_node_map_empty_for_none_workflow():
    assert ws.slot_node_map(None) == {}


# ---------- 2. GET 派生工作台 ----------

def test_get_workspace_derived(client, tmp_path):
    ctx = _seed(client, tmp_path)
    r = client.get("/api/shots/{}/regenerate/workspace".format(ctx["shot1"]), params={"target": "clip"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "derived"
    assert len(data["available_slots"]) == 9
    assert {s["param"] for s in data["available_slots"]} == set(IMAGE_SLOTS)
    assert data["capabilities"]["spatial_control"] == "prompt_level"
    assert data["capabilities"]["timeline_control"] == "prompt_level"
    assert "continuity" in data
    assert data["history_compat"]["has_saved_config"] is False
    assert data["history_compat"]["base_snapshot_complete"] is True
    assert len(data["references"]) == 2
    assert {x["slot_id"] for x in data["references"]} == {"ref_identity", "ref_character_2"}
    for ref in data["references"]:
        assert ref["node_id"] is not None
        # 图序 = 实际注入槽位压缩后的连续编号（2026-11-21 修复）。
        # 此前断言 (3, 4) 是工作流**静态**编号：ref_identity 静态第 3、
        # ref_character_2 静态第 4。但本镜没有上一镜尾帧 → ref_prev_frame 空
        # → 执行器压缩后这两张实际是模型收到的第 1、2 张。
        # 页面显示静态编号就会与提示词里的 <Picture 1> 对不上。
        assert ref["image_no"] in (1, 2)


# ---------- 3. PUT 保存与版本递增 ----------

def test_put_workspace_saves_and_version_increments(client, tmp_path):
    ctx = _seed(client, tmp_path)
    url = "/api/shots/{}/regenerate/workspace".format(ctx["shot1"])
    got = client.get(url, params={"target": "clip"}).json()
    payload = {
        "target": "clip",
        "references": got["references"],
        "character_bindings": got["character_bindings"],
        "spatial_layout": got["spatial_layout"],
        "action_timeline": got["action_timeline"],
        "prop_bindings": got["prop_bindings"],
        "prompt_sections": got["prompt_sections"],
        "final_prompt": "用户手写提示词",
    }
    r1 = client.put(url, json=payload)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["source"] == "saved"
    assert d1["history_compat"]["saved_version"] == 1
    # 详情页的提示词会补上【参考图职责】块（图序与槽位一一对应），
    # 用户正文必须原样保留在末尾
    assert "用户手写提示词" in d1["final_prompt"]
    assert "【参考图职责】" in d1["final_prompt"]

    r2 = client.put(url, json=payload)
    assert r2.status_code == 200
    assert r2.json()["history_compat"]["saved_version"] == 2

    d3 = client.get(url, params={"target": "clip"}).json()
    assert d3["source"] == "saved"
    assert d3["history_compat"]["saved_version"] == 2
    assert len(d3["references"]) == 2
    assert "用户手写提示词" in d3["final_prompt"]
    assert "【参考图职责】" in d3["final_prompt"]


# ---------- 4. 校验门禁 ----------

def test_workspace_validate_blocks_prompt_ref_mismatch(client, tmp_path):
    ctx = _seed(client, tmp_path)
    url = "/api/shots/{}/regenerate/workspace/validate".format(ctx["shot1"])
    got = client.get("/api/shots/{}/regenerate/workspace".format(ctx["shot1"]),
                     params={"target": "clip"}).json()
    payload = {
        "target": "clip",
        "references": got["references"],
        "character_bindings": got["character_bindings"],
        "spatial_layout": got["spatial_layout"],
        "action_timeline": got["action_timeline"],
        "prop_bindings": got["prop_bindings"],
        "prompt_sections": got["prompt_sections"],
        "final_prompt": "镜头开始 <Picture 9> 角色走入画面。",
    }
    r = client.post(url, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False
    assert "PROMPT_REF_MISSING" in {e["code"] for e in body["errors"]}


# ---------- 5. 连续性校验 ----------

def test_build_continuity_first_shot(client, tmp_path):
    from app.models import Shot
    ctx = _seed(client, tmp_path, two_shots=False)
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        shot = db.get(Shot, ctx["shot1"])
        res = ws.build_continuity(db, shot, current_spatial=None, current_props=None)
        assert res["status"] == "first_shot"
        assert res["previous_shot_id"] is None
        assert res["auto_applied"] is False
    finally:
        db.close()


def test_build_continuity_prev_missing_structured_config(client, tmp_path):
    from app.models import Shot
    ctx = _seed(client, tmp_path)
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        shot = db.get(Shot, ctx["shot2"])
        res = ws.build_continuity(db, shot, current_spatial=None, current_props=None)
        assert res["previous_shot_id"] == ctx["shot1"]
        assert res["previous_config_found"] is False
        assert res["auto_applied"] is False
        assert "PREV_STATE_UNKNOWN" in [i["code"] for i in res["issues"]]
        assert res["summary"]
    finally:
        db.close()


def test_build_continuity_detects_position_jump(client, tmp_path):
    from app.models import Shot
    ctx = _seed(client, tmp_path)
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        prev_spatial = {
            "characters": [{
                "character_id": "chr_test_1", "character_name": "小林",
                "initial_position": {"status": "confirmed", "horizontal": "left",
                                     "vertical": "center", "depth": "foreground",
                                     "facing_direction": "right"},
                "final_position": {"status": "confirmed", "horizontal": "left",
                                   "vertical": "center", "depth": "foreground",
                                   "facing_direction": "right"},
            }],
        }
        db.add(StoryboardRegenConfig(
            id="rgc_test_prev", project_id=ctx["project_id"], shot_id=ctx["shot1"],
            target="clip", workflow_id="wf_h3_real", workflow_version=7, version=1,
            references=[], character_bindings=[], spatial_layout=prev_spatial,
            action_timeline={}, prop_bindings=[], prompt_sections={},
            final_prompt="上一镜提示词", parameters={}, extra={},
            created_at=regen._now(),
        ))
        db.commit()

        cur_spatial = {
            "characters": [{
                "character_id": "chr_test_1", "character_name": "小林",
                "initial_position": {"status": "confirmed", "horizontal": "right",
                                     "vertical": "center", "depth": "foreground",
                                     "facing_direction": "right"},
                "final_position": {"status": "pending_confirmation"},
            }],
        }
        shot = db.get(Shot, ctx["shot2"])
        res = ws.build_continuity(db, shot, current_spatial=cur_spatial, current_props=[])
        assert res["previous_config_found"] is True
        assert "POSITION_JUMP" in [i["code"] for i in res["issues"]]
        assert res["auto_applied"] is False
        assert res["summary"]
    finally:
        db.close()


def test_continuity_section_filled_in_prompt_sections(client, tmp_path):
    from app.models import Shot, Workflow
    ctx = _seed(client, tmp_path)
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        db.add(StoryboardRegenConfig(
            id="rgc_test_prev2", project_id=ctx["project_id"], shot_id=ctx["shot1"],
            target="clip", workflow_id="wf_h3_real", workflow_version=7, version=1,
            references=[], character_bindings=[], spatial_layout={"characters": []},
            action_timeline={}, prop_bindings=[], prompt_sections={},
            final_prompt="上一镜", parameters={}, extra={}, created_at=regen._now(),
        ))
        db.commit()
        shot = db.get(Shot, ctx["shot2"])
        wf = db.get(Workflow, "wf_h3_real")
        asset = db.get(Asset, "ast_clip_002")
        base_config = regen.build_base_config(db, asset)
        data = ws.build_workspace(
            db, shot=shot, target="clip", workflow=wf, base_config=base_config, saved=None,
        )
        assert data["continuity"]["status"] in ("ok", "conflict", "first_shot")
        assert data["prompt_sections"]["continuity"] or data["continuity"]["status"] == "first_shot"
    finally:
        db.close()


# ---------- 6. 已保存配置读取 ----------

def test_load_saved_config_returns_none_when_absent(client, tmp_path):
    ctx = _seed(client, tmp_path, two_shots=False)
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        assert ws.load_saved_config(db, ctx["shot1"], "clip") is None
        assert ws.load_saved_config(db, "", "") is None
    finally:
        db.close()


def test_load_saved_config_returns_saved_payload(client, tmp_path):
    ctx = _seed(client, tmp_path, two_shots=False)
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        db.add(StoryboardRegenConfig(
            id="rgc_test_load", project_id=ctx["project_id"], shot_id=ctx["shot1"],
            target="clip", workflow_id="wf_h3_real", workflow_version=7, version=4,
            references=[{"slot_id": "ref_identity", "asset_id": "ast_ws_ref_000"}],
            character_bindings=[], spatial_layout={"characters": []},
            action_timeline={}, prop_bindings=[], prompt_sections={"scene": "草地"},
            final_prompt="保存的提示词", parameters={"width": 736}, extra={"k": "v"},
            created_at=regen._now(),
        ))
        db.commit()
        saved = ws.load_saved_config(db, ctx["shot1"], "clip")
        assert saved is not None
        assert saved["version"] == 4
        assert saved["final_prompt"] == "保存的提示词"
        assert saved["parameters"]["width"] == 736
        assert len(saved["references"]) == 1
        assert saved["prompt_sections"]["scene"] == "草地"
    finally:
        db.close()


# ---------- 7. 目标与参数 ----------

def test_workspace_rejects_unknown_target(client, tmp_path):
    ctx = _seed(client, tmp_path, two_shots=False)
    r = client.get("/api/shots/{}/regenerate/workspace".format(ctx["shot1"]),
                   params={"target": "unknown"})
    assert r.status_code == 400


def test_workspace_requires_existing_base_asset(client, tmp_path):
    ctx = _seed(client, tmp_path, two_shots=False)
    r = client.get("/api/shots/{}/regenerate/workspace".format(ctx["shot1"]),
                   params={"target": "keyframe"})
    assert r.status_code == 404


def test_workspace_parameters_snapshot_kept(client, tmp_path):
    ctx = _seed(client, tmp_path, two_shots=False)
    data = client.get("/api/shots/{}/regenerate/workspace".format(ctx["shot1"]),
                      params={"target": "clip"}).json()
    params = data["parameters"]
    assert params.get("width") == 736
    assert params.get("height") == 1312
    assert params.get("length") == 192
    assert "__media__" not in params


# ---------- 8. 从未生成过产物的镜头（待生成 → 视频生成中） ----------

def _seed_no_assets(client):
    """建项目/场景/镜头，但一张图、一个任务都没有。

    对应分镜看板的真实流程：用户要先打开待生成镜头的详情，调好提示词与参考图
    再提交。此时既没有成片也没有分镜图，接口不能 404——否则整条链路断掉。
    """
    resp = client.post("/api/projects", json={"name": "待生成镜头", "aspect_ratio": "9:16"})
    assert resp.status_code in (200, 201), resp.text
    pid = resp.json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "草地", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        wf = Workflow(
            id="wf_h3_noasset", name="wf_video_h3_ref2va_std_5s", workflow_type="video",
            definition=H3_DEF, capabilities=H3_CAPS, version=7, is_builtin=True,
        )
        db.add(wf)
        db.commit()
    finally:
        db.close()
    return pid, shot["id"]


def test_workspace_opens_for_shot_without_any_asset(client):
    """零产物镜头也必须能打开工作区，否则待生成镜头无法调整提示词与参考图。"""
    _, shot_id = _seed_no_assets(client)
    r = client.get("/api/shots/{}/regenerate/workspace".format(shot_id), params={"target": "clip"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["history_compat"]["fallback_from"] == "none"
    # 没有可复用的快照，必须如实标记不完整，不能把空参数当可信值
    assert data["snapshot_complete"] is False
    assert "base_snapshot_missing" in data["incomplete_reasons"]


def test_no_asset_workspace_prompt_has_no_dangling_picture_refs(client):
    """一张参考图都没有时，提示词里不得残留指向不存在图片的 <Picture N>。"""
    _, shot_id = _seed_no_assets(client)
    data = client.get("/api/shots/{}/regenerate/workspace".format(shot_id),
                      params={"target": "clip"}).json()
    enabled = [r for r in data["references"] if r.get("enabled", True)]
    tags = sorted({int(m) for m in __import__("re").findall(r"<Picture\s*(\d+)>", data["final_prompt"] or "")})
    assert tags == list(range(1, len(enabled) + 1)), (
        "参考图 {} 张，但提示词引用了 {}".format(len(enabled), tags)
    )


def _add_keyframe(tmp_path, project_id, shot_id):
    """给镜头补一张当前生效分镜图（来源任务成功），用于验证 clip 目标回退。"""
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        job = Job(
            id="job_kf_fallback", project_id=project_id, shot_id=shot_id,
            job_type="generate_keyframe", workflow_id="wf_h3_noasset", workflow_version=7,
            params={"prompt": "原分镜图提示词"}, status="succeeded", created_at=regen._now(),
            input_snapshot={"workflow_version": 7, "params": {"prompt": "原分镜图提示词"}},
        )
        db.add(job)
        db.flush()
        asset = Asset(
            id="ast_kf_fallback", project_id=project_id, asset_type="keyframe",
            name="storyboard_1_img", version=1,
            file_path=_media(tmp_path, "kf.png"), source_job_id=job.id, validity="current",
        )
        db.add(asset)
        db.commit()
        return asset.id
    finally:
        db.close()


def test_workspace_submit_allows_shot_without_any_asset(client):
    """零产物镜头提交必须成功：GET 能打开、POST 却 404 会让待生成镜头链路断掉。"""
    _, shot_id = _seed_no_assets(client)
    r = client.post(
        "/api/shots/{}/regenerate/workspace/regenerate".format(shot_id),
        json={"target": "clip", "final_prompt": "镜头开始，人物走入画面。"},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == "queued"
        snap = job.input_snapshot or {}
        assert snap.get("base_asset_id") is None
        assert snap.get("base_asset_version") is None
    finally:
        db.close()


def test_workspace_submit_falls_back_to_current_keyframe(client, tmp_path):
    """尚无分镜视频但有分镜图时，以分镜图为基准提交，不得报『尚无当前生效产物』。"""
    pid, shot_id = _seed_no_assets(client)
    kf_id = _add_keyframe(tmp_path, pid, shot_id)
    r = client.post(
        "/api/shots/{}/regenerate/workspace/regenerate".format(shot_id),
        json={"target": "clip", "final_prompt": "镜头开始，人物走入画面。"},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        snap = db.get(Job, job_id).input_snapshot or {}
        assert snap.get("base_asset_id") == kf_id
    finally:
        db.close()

