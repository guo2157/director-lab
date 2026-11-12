"""提示词与参考图一致性校验 + Qwen 多参考图槽位压缩测试。

覆盖两个此前零测试覆盖的关键场景：
1. 提示词引用错位阻断（validate_prompt_consistency）：
   - 引用编号无对应启用参考图 → PROMPT_REF_MISSING 错误（阻断生成）
   - 已注入未被引用 → REF_NOT_REFERENCED 警告（允许确认后生成）
   - 槽位存在但 enabled=False → 视为缺失，引用仍报错
   - 纯文生图（无图片引用语法）→ 直接通过
2. 删除参考图后槽位压缩不错位（compact_qwen_image_slots + remap_image_tags）：
   - images.image_N 断档压缩为连续编号（image_6 → image_3）
   - 提示词 <imageN> 按压缩结果重写
   - 引用已剔除槽位的行整行移除
3. 槽位图序换算（slot_image_index / workflow_ref_syntax）：
   - images.image_N → N（1-based）
   - ref_images.ref_image_N → N+1（0-based 转 1-based）
"""
from app.services.regeneration import validate_prompt_consistency
from app.services.workflow_inject import compact_qwen_image_slots, remap_image_tags
from app.services.ref_inject import slot_image_index, workflow_ref_syntax


# ---------- 槽位图序换算 ----------

QWEN_SLOTS = [
    {"param": "ref_scene", "input_key": "images.image_1"},
    {"param": "ref_identity", "input_key": "images.image_2"},
    {"param": "ref_character_2", "input_key": "images.image_3"},
    {"param": "ref_prop", "input_key": "images.image_6"},
]

H3_SLOTS = [
    {"param": "ref_identity", "input_key": "ref_images.ref_image_0"},
    {"param": "ref_scene", "input_key": "ref_images.ref_image_1"},
]


def test_slot_image_index_qwen_images():
    assert slot_image_index("ref_scene", QWEN_SLOTS) == 1
    assert slot_image_index("ref_identity", QWEN_SLOTS) == 2
    assert slot_image_index("ref_prop", QWEN_SLOTS) == 6
    assert slot_image_index("ref_unknown", QWEN_SLOTS) is None


def test_slot_image_index_h3_ref_images_zero_based():
    # ref_images.ref_image_N 为 0-based，换算为 1-based 图片序号
    assert slot_image_index("ref_identity", H3_SLOTS) == 1
    assert slot_image_index("ref_scene", H3_SLOTS) == 2


def test_workflow_ref_syntax_detection():
    assert workflow_ref_syntax(QWEN_SLOTS) == "image"
    assert workflow_ref_syntax(H3_SLOTS) == "picture"
    assert workflow_ref_syntax([{"param": "x", "input_key": None}]) is None
    assert workflow_ref_syntax([]) is None


# ---------- 提示词一致性校验 ----------


def _ref(slot_param, enabled=True):
    return {"slot_param": slot_param, "asset_id": "a-1", "enabled": enabled}


def test_consistency_ok_when_all_referenced():
    prompt = "以 <image1> 为场景底图，角色按 <image2> 取身份。"
    refs = [_ref("ref_scene"), _ref("ref_identity")]
    result = validate_prompt_consistency(prompt, refs, QWEN_SLOTS)
    assert result["valid"] is True
    assert result["errors"] == []
    assert result["warnings"] == []
    assert len(result["checks"]) == 2
    assert result["checks"][0]["slot"] == "ref_scene"
    assert result["checks"][1]["slot"] == "ref_identity"


def test_consistency_error_when_reference_missing():
    # 提示词引用 <image3>（ref_character_2 槽位）但该槽位未启用 → 阻断
    prompt = "角色按 <image2>、<image3> 取身份与服装。"
    refs = [_ref("ref_scene"), _ref("ref_identity")]
    result = validate_prompt_consistency(prompt, refs, QWEN_SLOTS)
    assert result["valid"] is False
    assert [e["code"] for e in result["errors"]] == ["PROMPT_REF_MISSING"]
    assert result["errors"][0]["image_no"] == 3


def test_consistency_disabled_ref_counts_as_missing():
    # 槽位存在但 enabled=False → 视为未启用，引用仍报错
    prompt = "以 <image1> 为场景底图，角色按 <image2> 取身份。"
    refs = [_ref("ref_scene"), _ref("ref_identity", enabled=False)]
    result = validate_prompt_consistency(prompt, refs, QWEN_SLOTS)
    assert result["valid"] is False
    assert [e["code"] for e in result["errors"]] == ["PROMPT_REF_MISSING"]
    assert result["errors"][0]["image_no"] == 2


def test_consistency_warning_when_ref_not_referenced():
    # 已注入但未被提示词引用 → 警告不阻断
    prompt = "以 <image1> 为场景底图重建画面。"
    refs = [_ref("ref_scene"), _ref("ref_identity")]
    result = validate_prompt_consistency(prompt, refs, QWEN_SLOTS)
    assert result["valid"] is True
    assert result["errors"] == []
    assert [w["code"] for w in result["warnings"]] == ["REF_NOT_REFERENCED"]
    assert result["warnings"][0]["slot"] == "ref_identity"
    assert result["warnings"][0]["image_no"] == 2


def test_consistency_passes_without_ref_syntax():
    # 纯文生图工作流（无 input_key）→ 无编号可校验，直接通过
    result = validate_prompt_consistency(
        "任意提示词 <image1>", [], [{"param": "p", "input_key": None}]
    )
    assert result["valid"] is True
    assert result["errors"] == []
    assert result["warnings"] == []
    assert result["checks"] == []


# ---------- Qwen 多参考图槽位压缩 ----------


def _qwen_workflow_with_images(image_values: dict) -> dict:
    """构造含 images.image_N 输入的 TextEncode 节点（值为 ComfyUI 连线列表）。"""
    inputs = {f"images.image_{n}": v for n, v in image_values.items()}
    return {
        "4": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {"prompt": "...", **inputs},
            "_meta": {"title": "文本编码"},
        }
    }


def test_compact_qwen_image_slots_renumbers_gap():
    # 删除 image_3/4/5 后断档：image_1, image_2, image_6 → 压缩为 1, 2, 3
    wf = _qwen_workflow_with_images({1: [20, 0], 2: [21, 0], 6: [25, 0]})
    out, mappings = compact_qwen_image_slots(wf)
    inputs = out["4"]["inputs"]
    assert "images.image_1" in inputs
    assert "images.image_2" in inputs
    assert "images.image_3" in inputs
    assert "images.image_6" not in inputs
    # 原 image_6（道具）的连线落到 image_3，不再被当作第 6 张图
    assert inputs["images.image_3"] == [25, 0]
    assert mappings == {"4": {1: 1, 2: 2, 6: 3}}


def test_compact_qwen_image_slots_noop_when_contiguous():
    wf = _qwen_workflow_with_images({1: [20, 0], 2: [21, 0], 3: [22, 0]})
    out, mappings = compact_qwen_image_slots(wf)
    assert out["4"]["inputs"]["images.image_1"] == [20, 0]
    assert out["4"]["inputs"]["images.image_2"] == [21, 0]
    assert out["4"]["inputs"]["images.image_3"] == [22, 0]
    assert mappings == {"4": {1: 1, 2: 2, 3: 3}}


def test_remap_image_tags_rewrites_numbers():
    # image_6 → image_3 后，提示词 <image6> 重写为 <image3>
    prompt = "以 <image1> 为场景底图；道具按 <image6> 取形状。"
    mappings = {"4": {1: 1, 2: 2, 6: 3}}
    new_prompt, dropped = remap_image_tags(prompt, mappings)
    assert "<image3>" in new_prompt
    assert "<image6>" not in new_prompt
    assert "<image1>" in new_prompt
    assert dropped == []


def test_remap_image_tags_drops_lines_referencing_removed_slot():
    # 引用已剔除槽位（image_2 被删除）的行整行移除，避免描述模型未收到的图片
    prompt = "以 <image1> 为场景底图。\n角色按 <image2> 取身份。\n道具按 <image6> 取形状。"
    mappings = {"4": {1: 1, 6: 2}}  # image_2 被剔除，image_6 → image_2
    new_prompt, dropped = remap_image_tags(prompt, mappings)
    assert "角色按" not in new_prompt
    assert "<image2>" in new_prompt  # 原 <image6> 重写为 <image2>
    assert dropped == [2]


def test_remap_image_tags_empty_mappings_noop():
    prompt = "以 <image1> 为场景底图。"
    new_prompt, dropped = remap_image_tags(prompt, {})
    assert new_prompt == prompt
    assert dropped == []
