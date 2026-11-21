"""回归测试：工作流 JSON 节点键名 ↔ capabilities 声明 ↔ 提示词编号必须同源。

用户提问（2026-11-21）：「提示词 <Picture N> 和参考图槽位，是不是也要看 H3
workflow json 里对应的 image 逻辑保持一致才行」——是，而且这是整条链的地基。

模型接收图片的顺序由 `definition[node].inputs["ref_images.ref_image_N"]` 的
N 决定；`capabilities.reference.slots[].input_key` 把业务槽位名映射到该键名；
提示词 `<Picture N>`（N = 键序号 + 1）告诉模型第 N 张图干什么用。

三份数据任何一份漂移，症状都是「编号与图片错位」。本文件把该约定变成可断言检查。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.workflow.slot_alignment import verify_reference_alignment

NODE = "6"


def _definition(count: int = 9) -> dict:
    """最小 H3 ref2va 节点：ref_images.ref_image_N → LoadImage 连线。"""
    inputs = {f"ref_images.ref_image_{i}": [str(30 + i), 0] for i in range(count)}
    return {NODE: {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": inputs}}


def _caps(pairs: list[tuple[str, str]]) -> dict:
    return {
        "reference": {
            "slots": [
                {"param": p, "input_key": k, "media_type": "image", "semantic": "x"}
                for p, k in pairs
            ]
        }
    }


ALIGNED = [
    ("ref_prev_frame", "ref_images.ref_image_0"),
    ("ref_image", "ref_images.ref_image_1"),
    ("ref_identity", "ref_images.ref_image_2"),
    ("ref_character_2", "ref_images.ref_image_3"),
]


def _codes(issues: list[dict], level: str | None = None) -> set[str]:
    return {i["code"] for i in issues if level is None or i["level"] == level}


def test_aligned_declaration_passes():
    assert verify_reference_alignment(_definition(), _caps(ALIGNED)) == []


def test_input_key_missing_from_workflow_is_an_error():
    """声明了节点上不存在的键：注入会静默落到空气里，模型收不到这张图。"""
    caps = _caps(ALIGNED + [("ref_scene", "ref_images.ref_image_9")])
    issues = verify_reference_alignment(_definition(), caps)
    assert "SLOT_INPUT_KEY_NOT_IN_WORKFLOW" in _codes(issues, "error")
    assert any(i.get("param") == "ref_scene" for i in issues)


def test_two_slots_sharing_one_key_is_an_error():
    """两个槽位抢同一个 input_key：后注入的覆盖前者，图序必然错位。"""
    caps = _caps(ALIGNED + [("ref_character_3", "ref_images.ref_image_3")])
    issues = verify_reference_alignment(_definition(), caps)
    assert "SLOT_INPUT_KEY_SHARED" in _codes(issues, "error")


def test_duplicate_param_is_an_error():
    caps = _caps(ALIGNED + [("ref_identity", "ref_images.ref_image_4")])
    assert "SLOT_PARAM_DUPLICATED" in _codes(verify_reference_alignment(_definition(), caps), "error")


def test_declaration_order_mismatch_is_a_warning_not_an_error():
    """声明顺序乱了：编号仍按键序号算（compressed_numbers 会排序），只是展示顺序不同。"""
    shuffled = [
        ("ref_image", "ref_images.ref_image_1"),
        ("ref_prev_frame", "ref_images.ref_image_0"),
    ]
    issues = verify_reference_alignment(_definition(), _caps(shuffled))
    assert "SLOT_DECLARATION_ORDER" in _codes(issues, "warning")
    assert not _codes(issues, "error")


def test_mixed_picture_and_image_syntax_is_a_warning():
    """同时声明 ref_images.* 与 images.*，提示词只能用一种标记，另一种必错。"""
    definition = _definition()
    definition[NODE]["inputs"]["images.image_1"] = ["50", 0]
    caps = _caps(ALIGNED + [("ref_prop", "images.image_1")])
    assert "SLOT_SYNTAX_MIXED" in _codes(verify_reference_alignment(definition, caps), "warning")


_DB = Path(__file__).resolve().parents[1] / "data" / "director_lab.db"


@pytest.mark.skipif(not _DB.exists(), reason="无本地数据库")
def test_real_video_workflows_are_aligned():
    """真实库里所有视频工作流必须 0 issue（防止换版工作流后静默漂移）。"""
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "select name, definition, capabilities from workflow where workflow_type='video'"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "库里没有视频工作流，测试无法验证"
    bad = {}
    for row in rows:
        issues = verify_reference_alignment(
            json.loads(row["definition"] or "{}"),
            json.loads(row["capabilities"] or "{}"),
        )
        if issues:
            bad[row["name"]] = [(i["level"], i["code"], i.get("param")) for i in issues]
    assert not bad, f"工作流槽位声明与 JSON 节点键名漂移：{bad}"
