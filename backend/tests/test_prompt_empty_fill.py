"""提示词编译的「空提示词填充」回归测试。

背景（实测 bug，2026-11-21）：
生成链路的入参组装是
``params.setdefault("prompt", shot.image_prompt or "")``。
当 ``shot.image_prompt`` 为空时，``prompt`` 键**已存在且为空串**，
于是 ``_compile_shot_prompt`` 里的 ``if "prompt" not in out`` 恒为 False，
流程走进 ``_apply_compiled_media``。该函数原本只合并参考图槽位、不碰提示词，
结果就是编译出的模板（1026 字，含「硬性要求」「画面风格」等分段）被整段丢弃，
工作流收到空提示词。

实测对象 sht_045190812cb5：修复前 ``params['prompt']`` 长度 0，
修复后 1026 字。

为什么这个 bug 在「两段式改造」后会变成必现路径：
改造的核心就是让 LLM 不再输出 image_prompt / video_prompt，改由
STORYBOARD_TEMPLATE / VIDEO_TEMPLATE 渲染。届时 image_prompt 恒为空，
每一镜都会踩到这条分支——所以必须在改造前修掉并锁住。
"""
from __future__ import annotations

import pytest


def _first_shot(db):
    from app.models import Scene, Shot

    return db.query(Shot).join(Scene, Scene.id == Shot.scene_id).first()


@pytest.fixture(autouse=True)
def _assume_asset_files_exist(monkeypatch):
    """隔离真实 media 目录：本文件只验证提示词填充与不被覆盖，
    不校验资产图片是否已落盘；否则用例会随 data/media 状态变化而飘。
    """
    from app.services import prompt_compiler as pc
    monkeypatch.setattr(pc, "_media_file_exists", lambda *_a, **_kw: True)


@pytest.fixture()
def ctx():
    """真实库上下文：取一个镜头 + 它的工作流。"""
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        import app.api.v1.generate as G

        shot = _first_shot(db)
        if shot is None:
            pytest.skip("库里没有镜头，跳过")
        from app.models import Scene

        project_id = db.get(Scene, shot.scene_id).project_id
        wf = G._match_workflow(db, "keyframe", None, project_id)
        if wf is None:
            pytest.skip("没有匹配的 keyframe 工作流，跳过")
        # 隔离真实资产质检状态：本文件只验证提示词填充与不被覆盖，
        # 不验证「质检未通过不得引用」的底图卫兵；否则用例会随资产质检结果飘。
        # 仅在会话内改为 pending，finally 中 rollback，不落库。
        from app.models import Asset
        for asset in db.query(Asset).filter(Asset.quality_status == "failed").all():
            asset.quality_status = "pending"
        db.flush()
        yield db, G, shot, wf
    finally:
        db.rollback()
        db.close()


def test_empty_image_prompt_gets_compiled_template(ctx):
    """image_prompt 为空时，必须用编译出的模板填充，不能下发空提示词。

    这是生产真实入参形态：``setdefault`` 已经把 prompt 设成空串。
    """
    db, G, shot, wf = ctx
    orig = shot.image_prompt
    shot.image_prompt = None
    try:
        params, snap = G._compile_shot_prompt(
            db, shot, wf, {"prompt": ""}, "keyframe", fallback_prompt=""
        )
    finally:
        shot.image_prompt = orig
        db.rollback()

    prompt = params.get("prompt") or ""
    assert prompt.strip(), "空 image_prompt 时 params['prompt'] 不能是空串（会下发空提示词）"
    # 模板特征：STORYBOARD_TEMPLATE v2 的固定分段（v2 以「必须保持」取代旧版「硬性要求」）
    assert "必须保持" in prompt, "填充内容应来自 STORYBOARD_TEMPLATE，而非其他来源"
    assert "画面风格" in prompt


def test_nonempty_image_prompt_is_not_overwritten(ctx):
    """已有提示词时不得覆盖：用户手改或重新生成时带的自定义提示词必须保留。"""
    db, G, shot, wf = ctx
    sentinel = "用户自定义提示词-必须原样保留-12345"
    params, _ = G._compile_shot_prompt(
        db, shot, wf, {"prompt": sentinel}, "keyframe", fallback_prompt=""
    )
    assert params.get("prompt") == sentinel


def test_blocked_compilation_raises_409_not_500(ctx, monkeypatch):
    """编译未就绪且自带提示词时，必须返回可读 409，不能抛裸 ValueError（500）。

    实测 bug（2026-11-22）：道具锚定图质检未通过 → 批量出图接口 500 且响应体为空，
    页面只能显示「批量生成失败」，用户无从知道是哪张参考图不合格。
    """
    from types import SimpleNamespace

    from app.services import prompt_compiler as pc

    db, G, shot, wf = ctx
    compiled = SimpleNamespace(
        status="needs_review",
        blocking_issues=[
            {
                "code": "reference_asset_quality_failed",
                "message": "道具锚定图质检未通过（疑似混入人物），需重新生成",
            }
        ],
        template_id="storyboard_image_v2",
    )
    monkeypatch.setattr(pc, "validate_compilation", lambda _payload: compiled)

    with pytest.raises(Exception) as ei:
        G._compile_shot_prompt(db, shot, wf, {"prompt": "已有提示词"}, "keyframe")

    exc = ei.value
    assert getattr(exc, "status_code", None) == 409, f"应返回 409，实际 {exc!r}"
    detail = getattr(exc, "detail", {}) or {}
    assert detail.get("code") == "prompt_compilation_blocked"
    assert "质检未通过" in detail.get("message", "")


def test_apply_compiled_media_fills_only_when_empty():
    """单元级锁定：_apply_compiled_media 只在 prompt 为空时填充。"""
    from types import SimpleNamespace

    import app.api.v1.generate as G

    compiled = SimpleNamespace(positive_prompt="模板渲染结果", reference_slots=[])

    # 空串 → 填充
    out = G._apply_compiled_media(compiled, {"prompt": ""})
    assert out["prompt"] == "模板渲染结果"

    # 只有空白 → 填充
    out = G._apply_compiled_media(compiled, {"prompt": "   \n "})
    assert out["prompt"] == "模板渲染结果"

    # 已有内容 → 不覆盖
    out = G._apply_compiled_media(compiled, {"prompt": "已有内容"})
    assert out["prompt"] == "已有内容"

    # 没有 prompt 键 → 填充
    out = G._apply_compiled_media(compiled, {})
    assert out["prompt"] == "模板渲染结果"


def test_apply_compiled_media_still_merges_slots():
    """填充提示词不能破坏原有职责：参考图槽位仍要合并。"""
    from types import SimpleNamespace

    import app.api.v1.generate as G

    compiled = SimpleNamespace(
        positive_prompt="模板",
        reference_slots=[
            {"slot_key": "ref_identity", "file_path": "/tmp/a.png"},
            {"slot_key": "ref_scene", "file_path": "/tmp/b.png"},
        ],
    )
    out = G._apply_compiled_media(compiled, {"prompt": "", "__media__": {"old": "/tmp/x.png"}})
    assert out["prompt"] == "模板"
    assert out["__media__"]["ref_identity"] == "/tmp/a.png"
    assert out["__media__"]["ref_scene"] == "/tmp/b.png"
    # 既有槽位不能被清掉
    assert out["__media__"]["old"] == "/tmp/x.png"
