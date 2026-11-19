"""多图参考重新生成工作台 V1 验收测试。

覆盖需求第六节九项中的核心判定：
1. 仅改提示词时参考图集合逐项不变
2. 替换角色参考图后仅该槽位变化
3. 移除可选道具参考图后不再注入该槽位
4. 移除必需槽位被明确阻止
5. 历史配置不完整时禁止默认模式
6. 参考图文件缺失 / 资产类型错配返回明确错误
7. 真实工作流槽位映射与帧约束语义区分
"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.database import make_engine
from app.models import Asset, Base, Job, Scene, Shot, Workflow
from app.services import regeneration as regen
from app.services.workflow.capabilities import normalize as normalize_capabilities

BACKEND_DIR = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = BACKEND_DIR / "data" / "workflows"

# 自动解析能力（不再依赖 .meta.json）
_H3_DEF = json.loads((WORKFLOW_DIR / "wf_video_h3_ref2va_std_5s.json").read_text("utf-8"))
_T2I_DEF = json.loads((WORKFLOW_DIR / "wf_image_t2i_qwen21_turbo.json").read_text("utf-8"))
_MULTI_DEF = json.loads((WORKFLOW_DIR / "wf_image_ref_qwen21_multi.json").read_text("utf-8"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    return TestClient(app)


def _media_file(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"fake-image-bytes")
    return str(p)


def _seed(client, tmp_path, slots_meta: dict, refs_files: list[str]):
    """创建项目/场景/镜头，并插入工作流、历史任务、基础版本资产与参考图资产。"""
    import app.core.database as db_mod

    pid = client.post("/api/projects", json={"name": "重生成测试", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "草地", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()

    db = db_mod.SessionLocal()
    try:
        wf = Workflow(
            id="wf_test_h3", name="wf_video_h3_ref2va", workflow_type="video",
            definition={"1": {"class_type": "X", "inputs": {}, "_meta": {"title": "$output.clip"}}},
            capabilities=slots_meta, version=7, is_builtin=True,
        )
        db.add(wf)
        db.flush()  # 先落库工作流，避免 job 外键因依赖顺序缺失而失败

        src_job = Job(
            id="job_base_0001", project_id=pid, shot_id=shot["id"], job_type="generate_clip",
            workflow_id=wf.id, workflow_version=7,
            params={"prompt": "原提示词", "width": 736, "height": 1312, "length": 192},
            status="succeeded",
            created_at=regen._now(),
            input_snapshot={
                "workflow_version": 7,
                "params": {"prompt": "原提示词", "width": 736, "height": 1312, "length": 192},
                "injected_media": {},
            },
        )
        db.add(src_job)

        base = Asset(
            id="ast_base_001", project_id=pid, asset_type="clip", name="基础版本",
            version=3, file_path=_media_file(tmp_path, "base.mp4"),
            source_job_id=src_job.id, validity="current",
        )
        db.add(base)

        ref_ids = []
        for idx, (slot, path) in enumerate(zip(["ref_identity", "ref_character_2", "ref_prop"], refs_files)):
            aid = f"ast_ref_{idx:03d}"
            db.add(Asset(
                id=aid, project_id=pid, asset_type="character" if slot != "ref_prop" else "prop",
                name=f"参考{idx}", version=2, file_path=path, validity="current",
            ))
            ref_ids.append(aid)
        db.commit()
    finally:
        db.close()

    return {"project_id": pid, "shot_id": shot["id"], "ref_ids": ref_ids}


H3_SLOTS = normalize_capabilities("video", _H3_DEF, capabilities=None, trigger=None)


def test_real_workflow_slots_are_authoritative():
    """以真实工作流元信息为准：H3 std_5s 九槽位映射与帧约束语义。

    槽位扩至 9 与 model_max_reference_images 对齐：
    ref_image_0=上一镜末帧、1=分镜图、2=身份、3=服装、4=道具、5-8=多角色多视角。
    """
    slots = regen.normalize_reference_slots(H3_SLOTS)
    by_param = {s["param"]: s for s in slots}
    assert set(by_param) == {
        "ref_prev_frame", "ref_image", "ref_identity", "ref_character_2", "ref_prop",
        "ref_character_3", "ref_character_4", "ref_character_5", "ref_character_6",
    }
    assert by_param["ref_prev_frame"]["input_key"] == "ref_images.ref_image_0"
    assert by_param["ref_image"]["input_key"] == "ref_images.ref_image_1"
    assert by_param["ref_identity"]["input_key"] == "ref_images.ref_image_2"
    # 尾帧是帧约束语义，不是普通参考图
    assert by_param["ref_prev_frame"]["semantic"] == "frame_constraint"
    assert by_param["ref_image"]["semantic"] == "reference"


def test_t2i_workflow_has_no_reference_slots():
    """t2i 工作流不声明任何参考槽位，不得凭空补造。"""
    caps = normalize_capabilities("image", _T2I_DEF, capabilities=None, trigger=None)
    assert regen.normalize_reference_slots(caps) == []


def test_qwen_multi_supports_nine_slots():
    """多参考图版声明 9 槽（单角色版 wf_image_ref_qwen21_turbo 已下线，由 multi 覆盖）。"""
    caps = normalize_capabilities("image", _MULTI_DEF, capabilities=None, trigger=None)
    slots = regen.normalize_reference_slots(caps)
    assert [s["param"] for s in slots] == [
        "ref_scene", "ref_identity", "ref_character_2",
        "ref_character_3", "ref_character_4", "ref_prop", "ref_prop_2",
        "ref_character_5", "ref_character_6",
    ]


def test_inherit_prompt_only_keeps_refs(client, tmp_path):
    """只改提示词：参考图集合与参数逐项保持不变。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        base = db.get(Asset, "ast_base_001")
        config = regen.build_base_config(db, base)
        assert config["base"]["version"] == 3
        assert config["workflow"]["version"] == 7
        assert config["prompt"]["rendered"] == "原提示词"

        base_refs = [
            {"slot_param": "ref_identity", "asset_id": ctx["ref_ids"][0]},
            {"slot_param": "ref_character_2", "asset_id": ctx["ref_ids"][1]},
            {"slot_param": "ref_prop", "asset_id": ctx["ref_ids"][2]},
        ]
        config["refs"] = base_refs
        snap = regen.assemble_snapshot(
            db, mode=regen.MODE_INHERIT_PROMPT_ONLY, base_config=config,
            prompt="新提示词", negative_prompt="",
        )
        # 提示词变化，参考图与参数不变
        assert snap["prompt"]["rendered"] == "新提示词"
        assert [r["slot_param"] for r in snap["refs"]] == ["ref_identity", "ref_character_2", "ref_prop"]
        assert snap["model_params"]["width"] == 736
        assert snap["model_params"]["length"] == 192
        assert snap["inherited"]["refs_locked"] is True
    finally:
        db.close()


def test_replace_character_ref_only_changes_that_slot(client, tmp_path):
    """替换角色参考图：仅该槽位资产变化，其他槽位保持原样。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        new_path = _media_file(tmp_path, "new_identity.png")
        db.add(Asset(
            id="ast_new_identity", project_id=ctx["project_id"], asset_type="character",
            name="新身份图", version=5, file_path=new_path, validity="current",
        ))
        db.commit()

        base = db.get(Asset, "ast_base_001")
        config = regen.build_base_config(db, base)
        refs = [
            {"slot_param": "ref_identity", "asset_id": "ast_new_identity"},
            {"slot_param": "ref_character_2", "asset_id": ctx["ref_ids"][1]},
            {"slot_param": "ref_prop", "asset_id": ctx["ref_ids"][2]},
        ]
        snap = regen.assemble_snapshot(
            db, mode=regen.MODE_MODIFY_REFS, base_config=config,
            prompt="原提示词", negative_prompt="", refs=refs,
        )
        by_slot = {r["slot_param"]: r for r in snap["refs"]}
        assert by_slot["ref_identity"]["asset_id"] == "ast_new_identity"
        assert by_slot["ref_identity"]["asset_version"] == 5
        assert by_slot["ref_character_2"]["asset_id"] == ctx["ref_ids"][1]
        assert snap["inherited"]["params_locked"] is True
    finally:
        db.close()


def test_remove_optional_slot_is_allowed(client, tmp_path):
    """移除可选槽位：该槽位不再出现在最终参考图与媒体参数中。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        base = db.get(Asset, "ast_base_001")
        config = regen.build_base_config(db, base)
        refs = [
            {"slot_param": "ref_identity", "asset_id": ctx["ref_ids"][0]},
            {"slot_param": "ref_character_2", "asset_id": ctx["ref_ids"][1], "enabled": False},
        ]
        snap = regen.assemble_snapshot(
            db, mode=regen.MODE_MODIFY_REFS, base_config=config,
            prompt="原提示词", negative_prompt="", refs=refs,
        )
        assert [r["slot_param"] for r in snap["refs"]] == ["ref_identity"]
        assert "ref_character_2" not in regen.snapshot_to_media(snap)
    finally:
        db.close()


def test_required_slot_removal_is_blocked(client, tmp_path):
    """必需槽位不可被移除或禁用。"""
    caps = json.loads(json.dumps(H3_SLOTS))
    caps["reference_slots"][0]["optional"] = False  # ref_prev_frame 设为必需
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, caps, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        slots = regen.normalize_reference_slots(caps)
        with pytest.raises(Exception) as exc:
            regen.validate_refs(db, slots, [
                {"slot_param": "ref_identity", "asset_id": ctx["ref_ids"][0]},
            ])
        assert "required_slot_missing" in str(exc.value)
    finally:
        db.close()


def test_missing_file_returns_explicit_error(client, tmp_path):
    """参考图文件丢失时返回明确错误，不静默通过。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)
    os.remove(files[1])

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        slots = regen.normalize_reference_slots(H3_SLOTS)
        with pytest.raises(Exception) as exc:
            regen.validate_refs(db, slots, [
                {"slot_param": "ref_identity", "asset_id": ctx["ref_ids"][0]},
                {"slot_param": "ref_character_2", "asset_id": ctx["ref_ids"][1]},
            ])
        assert "reference_file_missing" in str(exc.value)
    finally:
        db.close()


def test_type_mismatch_returns_explicit_error(client, tmp_path):
    """把场景资产塞进角色槽位时明确报错。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        slots = regen.normalize_reference_slots(H3_SLOTS)
        with pytest.raises(Exception) as exc:
            regen.validate_refs(db, slots, [
                {"slot_param": "ref_identity", "asset_id": ctx["ref_ids"][2]},
            ])
        assert "ref_type_mismatch" in str(exc.value)
    finally:
        db.close()


def test_incomplete_snapshot_blocks_default_mode(client, tmp_path):
    """基础版本缺少生成记录时，默认模式必须被阻止并提示不完整。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        # 历史版本缺少来源任务记录：配置无法反查，必须提示不完整
        db.add(Asset(
            id="ast_orphan_001", project_id=ctx["project_id"], asset_type="clip",
            name="无来源任务", version=1, file_path=files[0],
            source_job_id=None, validity="current",
        ))
        db.commit()
        orphan = db.get(Asset, "ast_orphan_001")
        config = regen.build_base_config(db, orphan)
        assert config["snapshot_complete"] is False
        assert "source_job_missing" in config["incomplete_reasons"]

        with pytest.raises(Exception) as exc:
            regen.assemble_snapshot(
                db, mode=regen.MODE_INHERIT_PROMPT_ONLY, base_config=config,
                prompt="x", negative_prompt="",
            )
        assert "snapshot_incomplete" in str(exc.value)
    finally:
        db.close()


def test_snapshot_hash_is_stable_and_frozen(client, tmp_path):
    """同一输入生成的快照哈希稳定，便于校验冻结内容未被篡改。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        base = db.get(Asset, "ast_base_001")
        config = regen.build_base_config(db, base)
        config["refs"] = [
            {"slot_param": "ref_identity", "asset_id": ctx["ref_ids"][0]},
            {"slot_param": "ref_character_2", "asset_id": ctx["ref_ids"][1]},
        ]
        snap1 = regen.assemble_snapshot(
            db, mode=regen.MODE_INHERIT_PROMPT_ONLY, base_config=config,
            prompt="同一提示词", negative_prompt="",
        )
        snap2 = regen.assemble_snapshot(
            db, mode=regen.MODE_INHERIT_PROMPT_ONLY, base_config=config,
            prompt="同一提示词", negative_prompt="",
        )
        assert snap1["snapshot_hash"] == snap2["snapshot_hash"]
        assert snap1["base"]["asset_id"] == "ast_base_001"
    finally:
        db.close()


def test_config_endpoint_exposes_base_version_and_slots(client, tmp_path):
    """接口返回基础版本、工作流版本、槽位与只读约束。"""
    files = [_media_file(tmp_path, f"r{i}.png") for i in range(3)]
    ctx = _seed(client, tmp_path, H3_SLOTS, files)

    r = client.get(f"/api/shots/{ctx['shot_id']}/regenerate/config", params={"target": "clip"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base"]["asset_id"] == "ast_base_001"
    assert body["base"]["version"] == 3
    assert body["workflow"]["version"] == 7
    assert body["editable"]["prompt"] is True
    assert body["editable"]["refs"] is False
    assert {s["param"] for s in body["available_slots"]} >= {"ref_identity", "ref_prop"}
