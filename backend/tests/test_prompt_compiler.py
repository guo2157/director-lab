"""阶段 B 提示词编译器测试（两阶段 LLM 设计）。

覆盖：模板渲染与占位校验、编译结果 JSON 严格校验、资产图隔离约束、
分镜图参考槽位映射与审核门禁、槽位能力上限、参考文件缺失、
视频连续性引用、『只改提示词』锁定字段。
"""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.database import make_engine
from app.models import Asset, Base, Character, Project, Prop, Scene, Shot, Workflow, ids


@pytest.fixture()
def db(tmp_path):
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'compiler.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _media(tmp_path, name: str) -> str:
    """生成真实存在的图片文件（绝对路径，供文件可读性校验通过）。"""
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return str(path)


def _workflow(capabilities: dict) -> Workflow:
    return Workflow(
        id=ids.workflow_id(),
        workflow_type="image",
        version=1,
        capabilities=capabilities,
        definition={"nodes": []},
        is_default=True,
    )


def _project(db, tmp_path, *, review="approved") -> Project:
    p = Project(id=ids.project_id(), name="编译器测试", creation_type="novel",
                aspect_ratio="9:16", status="active", assets_review_status=review)
    db.add(p)
    db.commit()
    return p


def _character(db, project, tmp_path, *, name="小马", portrait=True) -> Character:
    portrait_id = None
    if portrait:
        a = Asset(id=ids.asset_id(), project_id=project.id, asset_type="character",
                  name=name, version=1, validity="current",
                  file_path=_media(tmp_path, f"{name}.png"))
        db.add(a)
        db.flush()
        portrait_id = a.id
    c = Character(id=ids.character_id(), project_id=project.id, name=name,
                  appearance="棕黄色毛发的拟人化卡通小马，鬃毛深棕色",
                  portrait_asset_id=portrait_id)
    db.add(c)
    db.commit()
    return c


def _scene(db, project, tmp_path, *, title="马棚", baseline=True) -> Scene:
    baseline_id = None
    if baseline:
        a = Asset(id=ids.asset_id(), project_id=project.id, asset_type="scene",
                  name=f"{title}-基准图", version=1, validity="current",
                  file_path=_media(tmp_path, f"{title}.png"))
        db.add(a)
        db.flush()
        baseline_id = a.id
    s = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1,
              title=title, description="木质结构的马棚，干草铺地",
              baseline_asset_id=baseline_id)
    db.add(s)
    db.commit()
    return s


def _shot(db, scene, character=None, *, seq=1, prompt="小马在 @马棚 里吃草") -> Shot:
    s = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=seq,
             character_id=character.id if character else None, shot_kind="visual",
             duration=5, image_prompt=prompt, keyframe_confirmed=False)
    db.add(s)
    db.commit()
    return s


# ---------- 模板渲染 ----------
def test_templates_render_without_residual_placeholders():
    from app.services.prompt_compiler import TEMPLATE_REGISTRY, render_template

    mapping = {
        "name": "小马", "identity": "棕黄鬃毛的卡通小马", "style": "童话绘本风格",
        "species": "拟人化小马", "age_appearance": "少年", "face_and_head": "圆脸大眼",
        "hair_or_head": "深棕鬃毛", "body_shape": "匀称", "clothing": "蓝色背带裤",
        "fixed_accessories": "无", "main_colors": "棕黄与深棕",
        "distinctive_features": "额前白色星斑", "empty_expr": "空无一人，整个画面只由固定环境元素与天空构成", "foreground": "干草地面与近处木栅栏", "midground": "木质马棚主体，明确高度与材质", "background": "远景树影与薄雾层次", "camera_shot": "24mm 广角，平视机位，一点透视", "texture": "电影感写实摄影，轻微胶片颗粒", "spatial_layout": "半open马棚",
        "time_and_weather": "晴天午后", "lighting": "柔和侧光", "palette": "暖棕",
        "fixed_elements": "木栅栏", "excluded_props": "剧情道具", "shape": "方底口袋",
        "color": "麻黄", "material": "粗麻布", "relative_scale": "半人高",
        "shot_id": "sht_0001", "characters": "小马", "scene": "马棚", "props": "无",
        "action": "低头吃草", "positions": "居中", "prop_interaction": "无",
        "position_specs": "- 小马：站于木栅栏东侧，面朝南方",
        "standable_items": "木栅栏、石槽（可坐）、草垛",
        "opening_blocking": "小马站在木栅栏东侧，面朝南方",
        "gaze_targets": "小马低头吃草，视线自然落于地面",
        "character_positions_summary": "single character positioned according to the storyboard reference image",
        "emotion": "neutral", "shot_size": "中景", "camera_angle": "平视",
        "character_count": 1, "character_count_rule": "画面中的人物总数必须严格等于 1 人（小马），不多不少；每个人只出现一次，不出现分身、镜像、重影、背影复制",
        "composition": "三分法", "camera_movement": "固定机位",
        "character_slots": "ref_identity", "scene_slot": "ref_scene", "prop_slots": "无", "scene_target": "以 <image1> 为场景编辑底图", "ref_bindings": "<image1> 为场景；<image2> 为角色「小马」",
        "duration": "5", "start_state": "站立", "main_action": "转头", "end_state": "注视",
        "pacing": "平稳", "environment_changes": "无", "continuity_in_type": "none",
        "source_shot_id": "无", "source_frame_file_id": "无", "continuity_out_state": "延续",
        "dialogue": "(S1) <d>吃草</d> while the character speaks on screen", "soundscape": "风声", "music": "N/A",
        # 视频模板 v4 新增：按参考图绑定情况条件渲染的三段文案
        "continuity_lock": "This video must continue the approved storyboard reference image.",
        "spatial_lock": "Spatial layout is derived from the storyboard reference image.",
        "screen_geography": "小马 stays in the exact position defined by the storyboard reference image.",
    }
    for template_id in TEMPLATE_REGISTRY:
        rendered = render_template(template_id, mapping)
        assert rendered
        assert "{" not in rendered and "}" not in rendered


def test_template_missing_field_raises():
    from app.services.prompt_compiler import render_template

    with pytest.raises(ValueError, match="prompt_template_missing_field"):
        render_template("character_asset_prompt", {"name": "小马"})


def test_unknown_template_rejected():
    from app.services.prompt_compiler import render_template

    with pytest.raises(ValueError, match="unknown_template"):
        render_template("not_exists", {})


# ---------- 负向词合并 ----------
def test_merge_negative_deduplicates():
    from app.services.prompt_compiler import COMMON_NEGATIVE, merge_negative, negative_for

    merged = merge_negative("模糊，噪点", "模糊，水印")
    assert merged == "模糊，噪点，水印"
    character_negative = negative_for("character")
    assert COMMON_NEGATIVE.split("，")[0] in character_negative
    assert "多个角色" in character_negative
    # 合并不产生重复词
    terms = character_negative.split("，")
    assert len(terms) == len(set(terms))


# ---------- JSON 严格校验 ----------
def _valid_payload() -> dict:
    return {
        "status": "ready", "task_type": "asset_image", "asset_type": "character",
        "entity_id": "chr_0001", "template_id": "character_asset_prompt",
        "template_version": 1, "positive_prompt": "单角色全身定妆图",
        "negative_prompt": "模糊", "reference_slots": [],
        "generation_constraints": {}, "blocking_issues": [], "warnings": [],
        "canonical_snapshot": {},
    }


def test_validate_compilation_accepts_valid_payload():
    from app.services.prompt_compiler import validate_compilation

    compiled = validate_compilation(_valid_payload())
    assert compiled.status == "ready"


def test_validate_compilation_rejects_blank_positive_prompt():
    from app.services.prompt_compiler import validate_compilation

    payload = _valid_payload()
    payload["positive_prompt"] = "   "
    with pytest.raises(ValidationError):
        validate_compilation(payload)


def test_validate_compilation_rejects_duplicate_slots():
    from app.services.prompt_compiler import validate_compilation

    slot = {"slot_key": "ref_identity", "entity_id": "chr_1", "entity_type": "character",
            "approved_asset_file_id": "ast_1", "file_path": "/tmp/a.png",
            "role": "character_identity_reference", "required": True, "input_key": None}
    payload = _valid_payload()
    payload["reference_slots"] = [slot, dict(slot)]
    with pytest.raises(ValidationError):
        validate_compilation(payload)


def test_validate_compilation_rejects_unknown_field():
    from app.services.prompt_compiler import validate_compilation

    payload = _valid_payload()
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        validate_compilation(payload)


# ---------- 资产图隔离约束 ----------
def test_compile_character_asset_is_single_subject(db, tmp_path):
    from app.services.prompt_compiler import compile_asset_image

    project = _project(db, tmp_path)
    character = _character(db, project, tmp_path)
    compiled = compile_asset_image(db, "character", character.id)
    assert compiled.status == "ready"
    assert "只能有一个主体" in compiled.positive_prompt
    assert "全身" in compiled.positive_prompt
    assert compiled.generation_constraints["isolation_rule"] == "single_character_only"
    assert "多个角色" in compiled.negative_prompt
    assert compiled.reference_slots == []


def test_compile_scene_asset_excludes_characters(db, tmp_path):
    from app.services.prompt_compiler import compile_asset_image

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    compiled = compile_asset_image(db, "scene", scene.id)
    assert "空无一人、荒无人烟" in compiled.positive_prompt
    # cfg=1 下负向词无效，否定句已从正向提示词移除，改为正向穷举句式
    assert "不出现人物、行人、车辆或动物" not in compiled.positive_prompt
    assert "整个画面只由环境本身与天空构成" in compiled.positive_prompt
    assert "中景必须保留一段可见的干净地面与纵深" in compiled.positive_prompt
    assert "整个画面只由" in compiled.positive_prompt
    assert compiled.generation_constraints["isolation_rule"] == "environment_only_no_characters"
    assert "人物" in compiled.negative_prompt


def test_compile_prop_asset_excludes_hands(db, tmp_path):
    from app.services.prompt_compiler import compile_asset_image

    project = _project(db, tmp_path)
    prop = Prop(id=ids.prop_id(), project_id=project.id, name="麦子口袋",
                description="粗麻布口袋，装满金黄麦子")
    db.add(prop)
    db.commit()
    compiled = compile_asset_image(db, "prop", prop.id)
    assert "只能有一件道具" in compiled.positive_prompt
    assert compiled.generation_constraints["isolation_rule"] == "single_prop_only_no_hands"
    assert "手" in compiled.negative_prompt


def test_compile_asset_name_only_falls_back_to_name(db, tmp_path):
    """仅有名称时按既有资产模板语义以名称兜底：不阻断，只留告警。"""
    from app.services.prompt_compiler import compile_asset_image

    project = _project(db, tmp_path)
    prop = Prop(id=ids.prop_id(), project_id=project.id, name="空道具")
    db.add(prop)
    db.commit()
    compiled = compile_asset_image(db, "prop", prop.id)
    assert compiled.status == "ready"
    assert not compiled.blocking_issues
    assert any("asset_identity_fallback_to_name" in w for w in compiled.warnings)
    assert "空道具" in compiled.positive_prompt


def test_compile_asset_without_identity_is_blocked(db, tmp_path, monkeypatch):
    """名称与外观全空（真实缺身份）时才阻断，不虚构外观。"""
    from types import SimpleNamespace

    from app.services.prompt_compiler import compile_asset_image

    stub = SimpleNamespace(id="prop_stub", name=None, description=None, appearance=None)
    monkeypatch.setattr(db, "get", lambda model, pk: stub)
    compiled = compile_asset_image(db, "prop", "prop_stub")
    assert compiled.status == "needs_review"
    assert any(i["code"] == "asset_identity_missing" for i in compiled.blocking_issues)


# ---------- 分镜图槽位与门禁 ----------
def test_storyboard_blocked_without_asset_review(db, tmp_path):
    from app.services.prompt_compiler import compile_storyboard_image

    project = _project(db, tmp_path, review="pending")
    character = _character(db, project, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene, character)
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": ["ref_identity", "ref_scene"]}))
    assert compiled.status == "needs_review"
    assert any(i["code"] == "assets_review_pending" for i in compiled.blocking_issues)


def test_storyboard_slots_map_to_approved_assets(db, tmp_path):
    from app.services.prompt_compiler import compile_storyboard_image

    project = _project(db, tmp_path)
    character = _character(db, project, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene, character)
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": [
            {"param": "ref_identity", "input_key": "ref_images.ref_image_2", "optional": False},
            {"param": "ref_scene", "input_key": "ref_images.ref_image_4", "optional": True},
        ]}))
    assert compiled.status == "ready"
    slots = {s["slot_key"]: s for s in compiled.reference_slots}
    assert set(slots) == {"ref_identity", "ref_scene"}
    assert slots["ref_identity"]["entity_id"] == character.id
    assert slots["ref_identity"]["approved_asset_file_id"] == character.portrait_asset_id
    assert slots["ref_identity"]["input_key"] == "ref_images.ref_image_2"
    assert slots["ref_scene"]["entity_id"] == scene.id
    assert slots["ref_scene"]["approved_asset_file_id"] == scene.baseline_asset_id


def test_storyboard_blocks_missing_portrait(db, tmp_path):
    from app.services.prompt_compiler import compile_storyboard_image

    project = _project(db, tmp_path)
    character = _character(db, project, tmp_path, portrait=False)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene, character)
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": ["ref_identity", "ref_scene"]}))
    assert compiled.status == "needs_review"
    assert any(i["code"] == "character_portrait_missing" for i in compiled.blocking_issues)


def test_storyboard_blocks_reference_file_missing(db, tmp_path):
    from app.services.prompt_compiler import compile_storyboard_image

    project = _project(db, tmp_path)
    character = _character(db, project, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene, character)
    db.get(Asset, character.portrait_asset_id).file_path = str(tmp_path / "not_exists.png")
    db.commit()
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": ["ref_identity", "ref_scene"]}))
    assert any(i["code"] == "reference_asset_file_missing" for i in compiled.blocking_issues)


def test_storyboard_blocks_slot_capacity_exceeded(db, tmp_path):
    from app.services.prompt_compiler import compile_storyboard_image

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    first = _character(db, project, tmp_path, name="小马")
    second = _character(db, project, tmp_path, name="老马")
    shot = _shot(db, scene, first)
    # 两个角色但工作流只声明一个角色槽位
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": ["ref_identity", "ref_scene"]}))
    assert compiled.status == "ready" or compiled.status == "needs_review"
    shot.dialogue_segments = [{"speaker_id": second.id, "text": "来吧"}]
    db.commit()
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": ["ref_identity", "ref_scene"]}))
    assert compiled.status == "needs_review"
    assert any(i["code"] == "reference_capacity_exceeded" for i in compiled.blocking_issues)


# ---------- 视频连续性与校验 ----------
def test_shot_video_blocked_without_confirmed_storyboard(db, tmp_path):
    from app.services.prompt_compiler import compile_shot_video

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene)
    compiled = compile_shot_video(db, shot.id, workflow=_workflow(
        {"fps": 24, "reference_slots": ["ref_prev_frame"]}))
    assert compiled.status == "needs_review"
    assert any(i["code"] == "storyboard_not_approved" for i in compiled.blocking_issues)


def test_shot_video_continuity_binds_source_shot_and_file(db, tmp_path):
    from app.services.prompt_compiler import compile_shot_video

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    first = _shot(db, scene, None, seq=1, prompt="空镜")
    first.keyframe_confirmed = True
    second = _shot(db, scene, None, seq=2, prompt="小马走过")
    second.keyframe_confirmed = True
    second.requires_prev_frame = True

    # 首镜尾帧资产（last_frame 类型，current 且文件存在）
    tail = Asset(id=ids.asset_id(), project_id=project.id, asset_type="last_frame",
                 name="首镜尾帧", version=1, validity="current",
                 file_path=_media(tmp_path, "tail.png"))
    db.add(tail)
    db.flush()
    first.last_frame_asset_id = tail.id
    # 两镜都需要当前有效分镜图，校验才会聚焦尾帧来源
    for shot in (first, second):
        db.add(Asset(id=ids.asset_id(), project_id=project.id, asset_type="keyframe",
                     name="分镜", version=1, validity="current", source_job_id=None,
                     file_path=_media(tmp_path, f"kf_{shot.id}.png")))
    db.commit()
    from app.models import Job
    for shot in (first, second):
        job = Job(id=ids.job_id(), project_id=project.id, shot_id=shot.id,
                  job_type="generate_keyframe", status="succeeded", params={},
                  created_at=datetime.now(UTC))
        db.add(job)
        db.flush()
        asset = (db.query(Asset).filter(Asset.asset_type == "keyframe",
                                        Asset.file_path.like(f"%kf_{shot.id}.png"))
                 .one())
        asset.source_job_id = job.id
    db.commit()

    compiled = compile_shot_video(db, second.id, workflow=_workflow(
        {"fps": 24, "frame_count_step": 17, "frame_count_offset": 5,
         "frame_count_range": [124, 362], "reference_slots": ["ref_prev_frame"]}))
    assert compiled.generation_constraints["continuity_in"]["type"] == "previous_shot_last_frame"
    assert compiled.generation_constraints["continuity_in"]["source_shot_id"] == first.id
    assert compiled.generation_constraints["continuity_in"]["source_file_id"] == tail.id
    slot = {s["slot_key"]: s for s in compiled.reference_slots}["ref_prev_frame"]
    assert slot["entity_id"] == first.id
    assert slot["approved_asset_file_id"] == tail.id


def test_shot_video_blocks_missing_prev_frame(db, tmp_path):
    from app.services.prompt_compiler import compile_shot_video

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene)
    shot.keyframe_confirmed = True
    shot.requires_prev_frame = True
    db.commit()
    compiled = compile_shot_video(db, shot.id, workflow=_workflow(
        {"fps": 24, "reference_slots": ["ref_prev_frame"]}))
    assert any(i["code"] == "prev_frame_missing" for i in compiled.blocking_issues)


def test_shot_video_frame_count_respects_workflow_constraints(db, tmp_path):
    from app.services.prompt_compiler import compile_shot_video

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene)
    shot.keyframe_confirmed = True
    shot.duration = 3
    db.commit()
    kf = Asset(id=ids.asset_id(), project_id=project.id, asset_type="keyframe",
               name="分镜", version=1, validity="current",
               file_path=_media(tmp_path, "kf.png"))
    db.add(kf)
    db.commit()
    compiled = compile_shot_video(db, shot.id, workflow=_workflow(
        {"fps": 24, "frame_count_step": 17, "frame_count_offset": 5,
         "frame_count_range": [124, 362], "reference_slots": []}))
    frames = compiled.generation_constraints["frame_count"]
    assert 124 <= frames <= 362
    assert (frames - 5) % 17 == 0


# ---------- 只改提示词模式 ----------
def test_prompt_only_patch_locks_generation_params():
    from app.services.prompt_compiler import assert_prompt_only_patch

    original = {"prompt": "旧提示词", "negative": "旧负向", "__media__": {"ref_identity": "/a.png"},
                "width": 736, "height": 1312, "length": 124, "seed": 42}
    merged = assert_prompt_only_patch(original, {"prompt": "新提示词"})
    assert merged["prompt"] == "新提示词"
    assert merged["width"] == 736 and merged["height"] == 1312
    assert merged["__media__"] == {"ref_identity": "/a.png"}

    for locked in ("__media__", "width", "height", "length", "seed"):
        with pytest.raises(ValueError, match="prompt_only_patch_locked_fields"):
            assert_prompt_only_patch(original, {locked: 1})


def test_apply_to_params_rejects_not_ready(db, tmp_path):
    """编译结果存在阻断项（needs_review）时拒绝写入参数。"""
    from app.services.prompt_compiler import CompiledPrompt, apply_to_params

    compiled = CompiledPrompt(
        task_type="asset_image",
        template_id="prop_asset_prompt",
        template_version=1,
        entity_id="prop_x",
        asset_type="prop",
        positive_prompt="生成一张独立的道具参考图",
        negative_prompt="低质量",
        blocking_issues=[{"code": "asset_identity_missing", "message": "缺少身份"}],
    )
    assert compiled.status == "needs_review"
    with pytest.raises(ValueError, match="prompt_compilation_not_ready"):
        apply_to_params(compiled, {})


def test_compile_unknown_entity_raises(db):
    from app.services.prompt_compiler import compile_asset_image

    with pytest.raises(LookupError):
        compile_asset_image(db, "character", "chr_not_exist")


# ---------- 用户数据花括号与模板侧占位校验 ----------
def test_render_template_allows_braces_in_user_data():
    """用户数据含 { } 属合法内容，不得被误判为残留占位符而阻断生成。"""
    from app.services.prompt_compiler import render_template

    mapping = {
        "name": "宝箱{一}", "identity": "木箱", "shape": "方形", "color": "棕",
        "material": "木", "relative_scale": "中", "distinctive_features": "锁扣{}",
        "style": "写实{柔和}",
    }
    rendered = render_template("prop_asset_prompt", mapping)
    assert "宝箱{一}" in rendered
    assert "写实{柔和}" in rendered


def test_render_template_still_rejects_missing_field():
    """放宽花括号探测不能把缺字段校验一起放空。"""
    from app.services.prompt_compiler import render_template

    mapping = {
        "name": "宝箱", "identity": "木箱", "shape": "方形",
        "material": "木", "relative_scale": "中", "distinctive_features": "锁扣",
        "style": "写实",
    }
    with pytest.raises(ValueError, match="prompt_template_missing_field"):
        render_template("prop_asset_prompt", mapping)


# ---------- 编译结果交叉校验 ----------
def test_validate_rejects_needs_review_without_blocking():
    """状态为 needs_review 却没有任何阻断项，属自相矛盾，必须拒绝。"""
    from app.services.prompt_compiler import validate_compilation

    payload = _valid_payload()
    payload["status"] = "needs_review"
    with pytest.raises(ValidationError, match="status_needs_review_requires_blocking"):
        validate_compilation(payload)


def test_validate_rejects_task_asset_type_mismatch():
    """视频任务配角色资产类型，语义不可能成立。"""
    from app.services.prompt_compiler import validate_compilation

    payload = _valid_payload()
    payload.update({"task_type": "shot_video", "asset_type": "character",
                    "template_id": "shot_video_prompt"})
    with pytest.raises(ValidationError, match="task_asset_type_mismatch"):
        validate_compilation(payload)


def test_validate_rejects_template_task_mismatch():
    """分镜任务套角色模板，等价于用错模板编译，必须拒绝。"""
    from app.services.prompt_compiler import validate_compilation

    payload = _valid_payload()
    payload.update({"task_type": "storyboard_image", "asset_type": "keyframe",
                    "template_id": "character_asset_prompt"})
    with pytest.raises(ValidationError, match="template_task_mismatch"):
        validate_compilation(payload)


def test_validate_flags_missing_reference_file():
    """注入了却不存在的参考图会污染输出，校验期即阻断。"""
    from app.services.prompt_compiler import validate_compilation

    payload = _valid_payload()
    payload["reference_slots"] = [{
        "slot_key": "ref_identity", "entity_id": "chr_1", "entity_type": "character",
        "approved_asset_file_id": "ast_1", "file_path": "/tmp/compiler_not_exist_xyz.png",
        "role": "character_identity_reference",
    }]
    compiled = validate_compilation(payload)
    assert any(i["code"] == "reference_asset_file_missing" for i in compiled.blocking_issues)


# ---------- 参数写入不覆盖调用方输入 ----------
def test_apply_to_params_keeps_existing_prompt():
    """调用方已显式给定 prompt 时不被编译结果覆盖，只补缺失项。"""
    from app.services.prompt_compiler import CompiledPrompt, apply_to_params

    compiled = CompiledPrompt(
        task_type="asset_image", template_id="prop_asset_prompt", template_version=1,
        entity_id="prop_x", asset_type="prop",
        positive_prompt="编译产物", negative_prompt="编译负向",
    )
    out = apply_to_params(compiled, {"prompt": "用户自己的提示词", "seed": 7})
    assert out["prompt"] == "用户自己的提示词"
    assert out["negative"] == "编译负向"
    assert out["seed"] == 7

    filled = apply_to_params(compiled, {})
    assert filled["prompt"] == "编译产物"


# ---------- 视频连续性与时长边界 ----------
def _attach_keyframe(db, project, shot, tmp_path):
    """给镜头挂一张当前有效分镜图（经 source_job_id → job.shot_id 关联）。"""
    from datetime import datetime

    from app.models import Job

    job = Job(id=ids.job_id(), project_id=project.id, shot_id=shot.id,
              job_type="generate_keyframe", status="succeeded", params={},
              created_at=datetime.now(UTC))
    db.add(job)
    db.flush()
    db.add(Asset(id=ids.asset_id(), project_id=project.id, asset_type="keyframe",
                 name="分镜", version=1, validity="current", source_job_id=job.id,
                 file_path=_media(tmp_path, f"kf_{shot.id}.png")))
    db.commit()


def test_shot_video_cross_scene_skips_prev_frame(db, tmp_path):
    """跨场景不注入上一镜尾帧（API.md §8），改由 start_state 文字衔接并告警。"""
    from app.services.prompt_compiler import compile_shot_video

    project = _project(db, tmp_path)
    scene_a = _scene(db, project, tmp_path, title="马棚")
    first = _shot(db, scene_a, None, seq=1)
    first.keyframe_confirmed = True
    _attach_keyframe(db, project, first, tmp_path)

    # 直接构造第二个场景并显式指定 seq=2：_scene 辅助函数内部固定 seq=1 且立即提交，
    # 事后再改 seq 会与 scene 的 (project_id, seq) 唯一约束冲突。
    scene_b = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=2,
                    title="小河", description="清澈的小河，两岸绿草",
                    baseline_asset_id=None)
    db.add(scene_b)
    db.commit()
    second = _shot(db, scene_b, None, seq=2)
    second.keyframe_confirmed = True
    second.requires_prev_frame = True
    _attach_keyframe(db, project, second, tmp_path)

    compiled = compile_shot_video(db, second.id, workflow=_workflow({
        "fps": 24, "frame_count_step": 17, "frame_count_offset": 5,
        "frame_count_range": [124, 362], "reference_slots": ["ref_prev_frame"]}))
    codes = [i["code"] for i in compiled.blocking_issues]
    assert "prev_frame_missing" not in codes
    assert any("prev_frame_skipped" in w for w in compiled.warnings)
    assert not [s for s in compiled.reference_slots if s["slot_key"] == "ref_prev_frame"]


def test_shot_video_short_duration_is_blocked(db, tmp_path):
    """低于工作流帧数下限时阻断，不静默把短镜头拉长。"""
    from app.services.prompt_compiler import compile_shot_video

    project = _project(db, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene, None, seq=1)
    shot.keyframe_confirmed = True
    shot.duration = 2
    db.commit()
    _attach_keyframe(db, project, shot, tmp_path)

    compiled = compile_shot_video(db, shot.id, workflow=_workflow({
        "fps": 24, "frame_count_step": 17, "frame_count_offset": 5,
        "frame_count_range": [124, 362], "reference_slots": []}))
    issue = next(i for i in compiled.blocking_issues
                 if i["code"] == "duration_below_workflow_min")
    assert issue["min_frames"] == 124
    assert issue["frames"] < 124
    assert compiled.status == "needs_review"


# ---------- 项目风格配置接入 ----------
def _set_style(project, **kwargs):
    from app.services import style_config
    merged = dict(project.settings or {})
    merged.update(style_config.default_style_config())
    merged.update(kwargs)
    project.settings = merged
    return project


def test_asset_prompt_uses_project_visual_style(db, tmp_path):
    from app.services import style_config
    from app.services.prompt_compiler import compile_asset_image
    project = _set_style(_project(db, tmp_path), visual_style="3d_cartoon")
    db.commit()
    character = _character(db, project, tmp_path)
    compiled = compile_asset_image(db, "character", character.id)
    expected = style_config.VISUAL_STYLES["3d_cartoon"]["prompt"]
    assert expected in compiled.positive_prompt
    assert compiled.generation_constraints["style_config"]["visual_style"] == "3d_cartoon"


def test_asset_prompt_falls_back_to_default_style_for_old_project(db, tmp_path):
    from app.services import asset_prompts
    from app.services.prompt_compiler import compile_asset_image
    project = _project(db, tmp_path)
    project.settings = {"episode_duration_target": 90}
    db.commit()
    scene = _scene(db, project, tmp_path)
    compiled = compile_asset_image(db, "scene", scene.id)
    assert asset_prompts._DEFAULT_STYLE in compiled.positive_prompt
    assert "style_config" in compiled.generation_constraints


def test_asset_prompt_applies_custom_style_requirements(db, tmp_path):
    from app.services.prompt_compiler import compile_asset_image
    project = _set_style(_project(db, tmp_path),
                         custom_style_requirements="避免恐怖与过度刺激内容")
    db.commit()
    prop = Prop(id=ids.prop_id(), project_id=project.id, name="麦子口袋",
                description="粗麻布口袋，装满金黄麦子")
    db.add(prop)
    db.commit()
    compiled = compile_asset_image(db, "prop", prop.id)
    assert "避免恐怖与过度刺激内容" in compiled.positive_prompt


def test_storyboard_prompt_uses_style_pacing_and_tone(db, tmp_path):
    from app.services import style_config
    from app.services.prompt_compiler import compile_storyboard_image
    project = _set_style(_project(db, tmp_path), visual_style="chinese_ink",
                         pacing="gentle", emotional_tone=["温馨"])
    db.commit()
    character = _character(db, project, tmp_path)
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene, character)
    compiled = compile_storyboard_image(db, shot.id, workflow=_workflow(
        {"reference_slots": [
            {"param": "ref_identity", "input_key": "ref_images.ref_image_2", "optional": False},
            {"param": "ref_scene", "input_key": "ref_images.ref_image_4", "optional": True},
        ]}))
    assert compiled.status == "ready"
    assert style_config.VISUAL_STYLES["chinese_ink"]["prompt"] in compiled.positive_prompt
    assert style_config.PACING["gentle"]["prompt"] in compiled.positive_prompt
    assert "情绪基调以温馨为主" in compiled.positive_prompt
    assert compiled.generation_constraints["style_config"]["pacing"] == "gentle"
    assert compiled.canonical_snapshot["template_version"] >= 1


def test_shot_video_prompt_uses_project_visual_style(db, tmp_path, monkeypatch):
    from app.services import style_config
    from app.services.prompt_compiler import compile_shot_video
    project = _set_style(_project(db, tmp_path), visual_style="cinematic_realism",
                         pacing="tight", emotional_tone=["紧张"])
    db.commit()
    scene = _scene(db, project, tmp_path)
    shot = _shot(db, scene)
    shot.keyframe_confirmed = True
    db.commit()
    monkeypatch.setattr(
        "app.services.prompt_compiler._media_file_exists", lambda path: True)
    db.add(Asset(id=ids.asset_id(), project_id=project.id, asset_type="keyframe",
                 name="分镜图", version=1, validity="current", file_path="/tmp/kf.png"))
    db.commit()
    compiled = compile_shot_video(db, shot.id, workflow=_workflow(
        {"fps": 24, "frame_count_range": [124, 362], "reference_slots": []}))
    assert style_config.VISUAL_STYLES["cinematic_realism"]["prompt"] in compiled.positive_prompt
    assert compiled.generation_constraints["style_config"]["visual_style"] == "cinematic_realism"
    assert compiled.canonical_snapshot["style"]["pacing"] == "tight"
