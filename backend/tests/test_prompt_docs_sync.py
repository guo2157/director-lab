"""文档与运行时提示词一致性防偏离测试。

约束：docs/prompts/*.md 是人类可读副本，运行时常量才是唯一事实来源。
任何一侧改动未同步，本测试即失败，避免文档悄悄偏离实现。
"""
from pathlib import Path

import pytest

from app.services import prompt_compiler as pc

DOCS = Path(__file__).resolve().parents[2] / "docs" / "prompts"


TEMPLATE_PAIRS = [
    ("compile_character_asset.md", pc.CHARACTER_TEMPLATE),
    ("compile_scene_asset.md", pc.SCENE_TEMPLATE),
    ("compile_prop_asset.md", pc.PROP_TEMPLATE),
    ("compile_storyboard_image.md", pc.STORYBOARD_TEMPLATE),
    ("compile_shot_video.md", pc.VIDEO_TEMPLATE),
]

NEGATIVE_PAIRS = [
    ("negative_common.md", pc.COMMON_NEGATIVE),
    ("negative_character.md", pc.CHARACTER_NEGATIVE),
    ("negative_scene.md", pc.SCENE_NEGATIVE),
    ("negative_prop.md", pc.PROP_NEGATIVE),
    ("negative_storyboard.md", pc.STORYBOARD_NEGATIVE),
    ("negative_video.md", pc.VIDEO_NEGATIVE),
]


@pytest.mark.parametrize("filename,expected", TEMPLATE_PAIRS)
def test_doc_template_matches_runtime(filename, expected):
    """docs 模板正文必须与运行时模板常量完全一致。"""
    path = DOCS / filename
    assert path.exists(), f"缺少文档模板 {filename}"
    assert path.read_text(encoding="utf-8").strip() == expected.strip(), (
        f"{filename} 与运行时常量不一致，请同步文档或实现"
    )


@pytest.mark.parametrize("filename,expected", NEGATIVE_PAIRS)
def test_doc_negative_matches_runtime(filename, expected):
    """docs 负向词必须与运行时负向常量一致。"""
    path = DOCS / filename
    assert path.exists(), f"缺少负向词文档 {filename}"
    assert path.read_text(encoding="utf-8").strip() == expected.strip(), (
        f"{filename} 与运行时负向常量不一致"
    )


def test_template_registry_complete():
    """五类模板标识必须在 TEMPLATE_REGISTRY 中登记且带版本号。"""
    expected = {
        pc.TEMPLATE_CHARACTER,
        pc.TEMPLATE_SCENE,
        pc.TEMPLATE_PROP,
        pc.TEMPLATE_STORYBOARD,
        pc.TEMPLATE_VIDEO,
    }
    assert expected <= set(pc.TEMPLATE_REGISTRY)
    for tid in expected:
        meta = pc.TEMPLATE_REGISTRY[tid]
        assert isinstance(meta.get("version"), int) and meta["version"] >= 1
        assert meta.get("prompt_name")


def test_asset_template_type_mapping():
    """三类资产必须映射到各自的模板标识，不允许共用。"""
    assert pc.ASSET_TEMPLATE_BY_TYPE["character"] == pc.TEMPLATE_CHARACTER
    assert pc.ASSET_TEMPLATE_BY_TYPE["scene"] == pc.TEMPLATE_SCENE
    assert pc.ASSET_TEMPLATE_BY_TYPE["prop"] == pc.TEMPLATE_PROP
    assert len(set(pc.ASSET_TEMPLATE_BY_TYPE.values())) == 3


def test_negative_layers_are_distinct():
    """通用层与各类型专用层不得完全相同，避免隔离约束退化为同一段文本。"""
    layers = [pc.CHARACTER_NEGATIVE, pc.SCENE_NEGATIVE, pc.PROP_NEGATIVE,
              pc.STORYBOARD_NEGATIVE, pc.VIDEO_NEGATIVE]
    assert len(set(layers)) == len(layers)
    assert pc.COMMON_NEGATIVE not in layers
