"""场景纯净度前移拦截测试（2026-11-02）。

背景：项目没有图生图（edit/inpaint）能力，人物一旦混进场景基准图，
唯一的补救手段是整图重采样。因此必须在生成后立刻拦截：
image_quality 新增 _detect_person（YCbCr 肤色掩码 + 16x16 网格连通块），
只对 scene/prop 生效，命中即写入 issues 触发换 seed 重采样。

这些用例刻意使用确定性合成图（PIL effect_noise 铺底噪点），
避免随机纹理导致偶发通过/失败。
"""
from PIL import Image, ImageDraw

from app.services.image_quality import _detect_person, check_image_quality

W, H = 768, 768
SKIN = (232, 180, 150)


def _make(path, base, blocks=()):
    """生成一张带确定性噪点的合成图，避免被判为纯色或文件过小。"""
    img = Image.new("RGB", (W, H), base)
    noise = Image.effect_noise((W, H), 18).convert("L")
    img = Image.merge(
        "RGB", [Image.blend(img.getchannel(i), noise, 0.12) for i in range(3)]
    )
    draw = ImageDraw.Draw(img)
    for box, color in blocks:
        draw.rectangle(box, fill=color)
    img.save(path, "PNG")
    return str(path)


def _person_cards():
    """躯干 + 头部组成的人物块，位置取画面中部。"""
    return [
        ((300, 260, 470, 620), SKIN),
        ((345, 180, 425, 260), SKIN),
    ]


def test_person_block_in_scene_is_rejected(tmp_path):
    """场景图里混入人物块 → 判失败并给出『疑似混入人物』。"""
    path = _make(tmp_path / "scene_person.png", (150, 180, 215), _person_cards())
    result = check_image_quality(path, asset_type="scene")
    assert result["passed"] is False
    assert any("疑似混入人物" in issue for issue in result["issues"]), result
    assert result["checks"]["person_check"] != "ok"


def test_empty_scene_passes_person_check(tmp_path):
    """空场景（天空 + 草地）不触发人物判定。"""
    path = _make(
        tmp_path / "scene_empty.png",
        (140, 175, 210),
        [((0, 480, W, H), (95, 135, 85))],
    )
    result = check_image_quality(path, asset_type="scene")
    assert result["checks"]["person_check"] == "ok", result
    assert result["passed"] is True, result


def test_warm_full_frame_background_is_not_flagged(tmp_path):
    """整体暖色调画面（沙地/木墙）不误报为人物。"""
    path = _make(tmp_path / "scene_warm.png", (205, 170, 130))
    result = check_image_quality(path, asset_type="scene")
    assert result["checks"]["person_check"] == "ok", result


def test_character_asset_skips_person_check(tmp_path):
    """角色定妆图本来就有人，不参与人物判定。"""
    path = _make(tmp_path / "character.png", (150, 180, 215), _person_cards())
    result = check_image_quality(path, asset_type="character")
    assert result["checks"]["person_check"] == "not_applicable"
    assert not any("疑似混入人物" in issue for issue in result["issues"])


def test_prop_asset_also_checked(tmp_path):
    """道具锚定图同样要求画面无人（人物色块 + 手部入画都要拦）。"""
    path = _make(tmp_path / "prop_person.png", (120, 120, 130), _person_cards())
    result = check_image_quality(path, asset_type="prop")
    assert any("疑似混入人物" in issue for issue in result["issues"]), result


def test_detect_person_on_flat_dark_image_returns_none(tmp_path):
    """纯暗画面没有任何肤色像素，直接放过。"""
    img = Image.new("RGB", (128, 128), (10, 10, 12))
    assert _detect_person(img) is None


def test_detect_person_on_skin_block_returns_message(tmp_path):
    """直接调用 _detect_person：中间一块肤色即应给出提示文案。"""
    img = Image.new("RGB", (128, 128), (30, 40, 60))
    ImageDraw.Draw(img).rectangle((40, 30, 90, 100), fill=SKIN)
    msg = _detect_person(img)
    assert msg is not None and "疑似混入人物" in msg


def test_detect_person_ignores_thin_cream_strip(tmp_path):
    """一维浅色窄条不算人物。

    实测 bug（2026-11-22）：道具图 ast_25f8313d91c3 的书口（米白色页边）
    被识别成 1 格宽 × 10 格高的竖条，误报「疑似混入人物」，
    导致道具锚定图被判不合格并阻断所有引用该道具的分镜图。
    """
    img = Image.new("RGB", (128, 128), (30, 40, 60))
    # 128px / 16 格 = 8px 一格；宽 8px、高 80px 的米白窄条
    ImageDraw.Draw(img).rectangle((48, 16, 55, 95), fill=(230, 220, 195))
    assert _detect_person(img) is None


def test_detect_person_ignores_horizontal_band(tmp_path):
    """横向暖色条带不算人物（形状判据）。

    实测 bug（2026-11-24）：场景图 ast_90c242520e2e（城市天台）里湿水泥地的
    橙色霓虹反光构成 8 格宽 × 3 格高的横向条带，被判「混入人物 16 格 / 肤色 13.0%」，
    于是 compile_storyboard_image 整单阻断 → resolve_keyframe_prompt 回退到旧版
    单人提示词 → 分镜图里第二个角色根本没画出来。
    """
    img = Image.new("RGB", (128, 128), (30, 40, 60))
    # 8 格宽 × 3 格高 = 64×24 px 的横向肤色条带（高宽比 0.375 < 0.8）
    ImageDraw.Draw(img).rectangle((32, 60, 95, 83), fill=SKIN)
    assert _detect_person(img) is None


def test_detect_person_ignores_full_frame_wall(tmp_path):
    """铺满画幅的暖色墙面不算人物（形状判据）。

    实测 bug（2026-11-24）：场景图 ast_e42d88e0c876（水泥楼梯间）的米白色墙面
    占据 13×13 格（81% 画幅），被判「混入人物 103 格 / 肤色 36.1%」。
    """
    img = Image.new("RGB", (128, 128), (30, 40, 60))
    # 13×13 格 = 104×104 px 的大面积米白墙
    ImageDraw.Draw(img).rectangle((8, 8, 111, 111), fill=(232, 214, 196))
    assert _detect_person(img) is None


def test_detect_person_ignores_high_saturation_brass(tmp_path):
    """高饱和金属色（黄铜/古铜）不算人物（饱和度判据）。

    实测 bug（2026-11-24）：道具图 ast_9e52f5fe03bf（旧钥匙）是竖放的黄铜钥匙，
    色相与肤色重合、形状也竖长（4 格宽 × 12 格高，高宽比 3.0），前两条判据都拦不住。
    真肤色 S 中位数实测 46~48，黄铜钥匙 159 —— 高饱和是材质不是皮肤。
    """
    img = Image.new("RGB", (128, 128), (30, 40, 60))
    # 4 格宽 × 12 格高 = 32×96 px 的竖长黄铜色块（S≈160）
    ImageDraw.Draw(img).rectangle((48, 16, 79, 111), fill=(196, 150, 52))
    assert _detect_person(img) is None


def test_detect_person_still_flags_real_skin_after_shape_and_saturation_guards(tmp_path):
    """加了形状/饱和度判据后，真正的低饱和肤色人物块仍须被判出（防误放行回归）。"""
    img = Image.new("RGB", (128, 128), (30, 40, 60))
    # 躯干 + 头：竖长、不铺满、低饱和
    draw = ImageDraw.Draw(img)
    draw.rectangle((56, 56, 79, 119), fill=SKIN)   # 躯干 3 格宽 × 8 格高
    draw.rectangle((60, 24, 75, 55), fill=SKIN)    # 头 2 格宽 × 4 格高
    msg = _detect_person(img)
    assert msg is not None and "疑似混入人物" in msg, msg
