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
