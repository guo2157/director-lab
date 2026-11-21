"""图片工作流画布尺寸：分镜图/场景跟随项目画幅，角色/道具固定画幅。"""

from app.worker.executor import _image_canvas_size


def test_keyframe_and_scene_follow_project_aspect_ratio():
    assert _image_canvas_size("9:16") == (1024, 1792)
    assert _image_canvas_size("16:9") == (1792, 1024)
    assert _image_canvas_size("1:1") == (1344, 1344)
    assert _image_canvas_size("16:9", "scene") == (1792, 1024)


def test_character_and_prop_keep_fixed_canvas():
    assert _image_canvas_size("16:9", "character") == (1024, 1792)
    assert _image_canvas_size("16:9", "asset") == (1024, 1792)
    assert _image_canvas_size("9:16", "prop") == (1344, 1344)


def test_unknown_aspect_ratio_falls_back_to_portrait():
    assert _image_canvas_size(None) == (1024, 1792)
    assert _image_canvas_size("4:3") == (1024, 1792)
