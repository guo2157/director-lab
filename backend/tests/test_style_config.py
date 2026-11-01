"""剧本类型与风格配置（Project Style Config）测试。"""
from app.services import style_config as sc


def test_defaults_cover_minimum_fields():
    d = sc.default_style_config()
    for key in ("genre_tags", "narrative_styles", "visual_style", "pacing",
                "emotional_tone", "target_audience"):
        assert key in d
    assert d["visual_style"] == sc.DEFAULT_VISUAL_STYLE
    assert d["visual_style"] in sc.VISUAL_STYLES


def test_default_visual_style_equals_legacy_style_text():
    from app.services import asset_prompts
    assert sc.VISUAL_STYLES[sc.DEFAULT_VISUAL_STYLE]["prompt"] == asset_prompts._DEFAULT_STYLE


def test_old_project_without_style_fields_falls_back_to_default():
    cfg = sc.normalize_style_config({"episode_duration_target": 90})
    assert cfg["visual_style"] == sc.DEFAULT_VISUAL_STYLE
    assert cfg["genre_tags"] == []
    assert cfg["pacing"] == "balanced"


def test_legacy_keys_are_migrated():
    cfg = sc.normalize_style_config({
        "genre": "穿越",
        "audience": "幼儿园至小学二年级",
        "visual_style": "国风水墨",
        "dialogue_style": "生活化",
        "requirements": "避免恐怖内容",
    })
    assert cfg["genre_tags"] == ["穿越"]
    assert cfg["visual_style"] == "chinese_ink"
    assert cfg["target_audience"] == "kindergarten_primary_2"
    assert cfg["dialogue_style"] == "natural"
    assert cfg["custom_style_requirements"] == "避免恐怖内容"


def test_unknown_visual_style_is_reported_and_reset():
    cfg, issues = sc.validate_style_config({"visual_style": "不存在的风格"})
    assert cfg["visual_style"] == sc.DEFAULT_VISUAL_STYLE
    codes = [i["code"] for i in issues]
    assert "visual_style_unknown" in codes


def test_unknown_narrative_style_and_tone_are_dropped():
    cfg, issues = sc.validate_style_config(
        {"narrative_styles": ["不存在的风格", "悬念反转型"],
         "emotional_tone": ["不存在", "温馨"]})
    assert cfg["narrative_styles"] == ["悬念反转型"]
    assert cfg["emotional_tone"] == ["温馨"]
    codes = [i["code"] for i in issues]
    assert "narrative_style_unknown" in codes
    assert "emotional_tone_unknown" in codes


def test_custom_requirement_truncated_with_error_issue():
    cfg, issues = sc.validate_style_config({"custom_style_requirements": "字" * (sc.MAX_CUSTOM_LENGTH + 50)})
    assert len(cfg["custom_style_requirements"]) == sc.MAX_CUSTOM_LENGTH
    errs = [i for i in issues if i["code"] == "custom_style_too_long"]
    assert errs and errs[0]["severity"] == "error"


def test_custom_requirement_whitespace_is_flattened():
    cfg = sc.normalize_style_config({"custom_style_requirements": "第一行\n\n第二行   第三行"})
    assert "\n" not in cfg["custom_style_requirements"]
    assert cfg["custom_style_requirements"] == "第一行 第二行 第三行"


def test_child_audience_conflicts_are_reported():
    _, issues = sc.validate_style_config({
        "target_audience": "kindergarten_primary_2",
        "narrative_styles": ["黑暗压抑型"],
        "conflict_intensity": "high",
        "genre_tags": ["惊悚恐怖"],
    })
    codes = {i["code"] for i in issues}
    assert "narrative_conflicts_audience" in codes
    assert "conflict_conflicts_audience" in codes
    assert "genre_conflicts_audience" in codes


def test_contradictory_narrative_styles_warn_but_keep_order():
    cfg, issues = sc.validate_style_config(
        {"narrative_styles": ["黑暗压抑型", "温情治愈型"]})
    assert cfg["narrative_styles"] == ["黑暗压抑型", "温情治愈型"]
    assert any(i["code"] == "narrative_style_conflict" for i in issues)


def test_asset_prompt_uses_only_visual_style_and_custom():
    p = sc.compile_style_prompt(
        {"visual_style": "3d_cartoon", "pacing": "fast", "emotional_tone": ["热血"],
         "custom_style_requirements": "适合儿童"},
        task_type="character")
    assert sc.VISUAL_STYLES["3d_cartoon"]["prompt"] in p
    assert "额外风格要求：适合儿童" in p
    # 资产图不掺入剧情节奏与情绪
    assert sc.PACING["fast"]["prompt"] not in p
    assert "热血" not in p


def test_storyboard_prompt_includes_pacing_and_main_tone():
    p = sc.compile_style_prompt(
        {"visual_style": "chinese_ink", "pacing": "gentle",
         "emotional_tone": ["温馨", "治愈"]},
        task_type="storyboard")
    assert sc.VISUAL_STYLES["chinese_ink"]["prompt"] in p
    assert sc.PACING["gentle"]["prompt"] in p
    assert "情绪基调以温馨为主，辅以治愈" in p


def test_snapshot_is_complete_and_detached_from_source():
    src = {"visual_style": "anime_2d", "genre_tags": ["校园青春"]}
    snap = sc.style_snapshot(src, project_id="prj_demo")
    assert snap["version"] == sc.STYLE_CONFIG_VERSION
    assert snap["project_id"] == "prj_demo"
    assert snap["visual_style"] == "anime_2d"
    assert snap["visual_style_label"] == sc.VISUAL_STYLES["anime_2d"]["label"]
    assert snap["genre_tags"] == ["校园青春"]
    src["genre_tags"].append("篡改")
    assert snap["genre_tags"] == ["校园青春"]


def test_style_diff_detects_unconfigured_detected_tags():
    diffs = sc.compute_style_diff(
        {"genre_tags": ["童话故事"], "narrative_styles": ["寓教于乐型"]},
        {"detected_genre_tags": ["童话故事", "寓言故事"],
         "detected_narrative_styles": ["温情治愈型"]})
    fields = {d["field"] for d in diffs}
    assert "genre_tags" in fields
    assert "narrative_styles" in fields
    genre_diff = next(d for d in diffs if d["field"] == "genre_tags")
    assert genre_diff["detected"] == ["寓言故事"]


def test_style_diff_empty_when_llm_reports_nothing():
    assert sc.compute_style_diff({"genre_tags": ["童话故事"]}, {}) == []
    assert sc.compute_style_diff({}, None) == []


def test_llm_description_states_content_first():
    text = sc.describe_for_llm({"visual_style": "comic"})
    assert "主视觉风格" in text
    assert "不得新增人物" in text


def test_parse_context_block_requires_visual_style_text_and_detected_fields():
    block = sc.parse_context_block({"visual_style": "pixel_art"})
    assert sc.VISUAL_STYLES["pixel_art"]["prompt"] in block
    assert "detected_genre_tags" in block
    assert "detected_narrative_styles" in block


def test_options_payload_exposes_all_groups():
    opt = sc.options_payload()
    assert len(opt["visual_styles"]) == len(sc.VISUAL_STYLES)
    assert len(opt["genre_tags"]) == len(sc.GENRE_TAGS)
    assert {g["group"] for g in opt["genre_groups"]} == set(sc.GENRE_GROUPS)
    assert opt["defaults"] == sc.default_style_config()
    for item in opt["visual_styles"]:
        assert item["value"] and item["label"] and item["description"]


# ── 修复回归：枚举非法值提示、空配置差异、标签归一化 ──────────
def test_unknown_enum_values_are_reported_not_silent():
    """旧自由文本或拼写错误的单选枚举值须产生问题码，不能静默回退默认值。"""
    cfg, issues = sc.validate_style_config({
        "dialogue_style": "对白风格",
        "narration_mode": "旁白优先",
        "pacing": "ultra_fast",
        "target_audience": "小学生",
    })
    codes = {i["code"] for i in issues}
    assert "dialogue_style_unknown" in codes
    assert "narration_mode_unknown" in codes
    assert "pacing_unknown" in codes
    assert "target_audience_unknown" in codes
    # 非法值回退默认，不阻断保存
    assert cfg["dialogue_style"] == sc.default_style_config()["dialogue_style"]
    assert cfg["pacing"] == sc.default_style_config()["pacing"]


def test_known_enum_values_produce_no_issue():
    """合法枚举值不产生任何问题码，避免修复引入误报。"""
    _, issues = sc.validate_style_config({
        "dialogue_style": "child_friendly",
        "narration_mode": "voiceover",
        "pacing": "tight",
        "conflict_intensity": "low",
        "twist_density": "none",
        "target_audience": "teen",
    })
    assert issues == []


def test_style_diff_skips_when_config_side_empty():
    """用户未配置题材/叙事风格时，模型识别结果不应被报成差异。"""
    diffs = sc.compute_style_diff(
        {},
        {"detected_genre_tags": ["校园青春", "友情成长"],
         "detected_narrative_styles": ["温情治愈型"]})
    assert diffs == []


def test_style_diff_tolerates_format_drift():
    """模型输出带空格、书名号等格式差异时，不应判为不一致。"""
    diffs = sc.compute_style_diff(
        {"genre_tags": ["童话故事"], "narrative_styles": ["寓教于乐型"]},
        {"detected_genre_tags": [" 童话故事 ", "《童话故事》"],
         "detected_narrative_styles": ["寓教于乐型 "]})
    assert diffs == []
