"""剧本类型与风格配置（Project Style Config）。
设计原则：
1. 题材（genre）、叙事风格（narrative）、视觉风格（visual）分开建模，不合并成单一下拉框；
2. 每个项目只有一个主视觉风格（visual_style 唯一），不允许各资产随机选风格；
3. 项目级配置是唯一基线：剧本分析 / 角色场景道具资产 / 分镜图 / 分镜视频读取同一份配置；
4. 风格只影响表达方式、构图、光影、节奏与情绪，不得新增人物、道具、事件或改变故事因果；
5. 视觉风格提示词片段来自本模块的 VISUAL_STYLES 模板表，前端不硬编码展示名称；
6. 旧项目缺少新字段时按旧默认风格回退，保证历史资产与已生成结果不会被悄悄改变。
兼容策略：
- 旧 Project.settings 里的 genre / audience / visual_style / dialogue_style / requirements
  会被映射到新字段；映射不到的自由文本作为校验提示返回，但不改变历史产物的风格；
- 旧默认美术风格（童话绘本写实风格）对应内部值 storybook_realism，其 prompt 与
  asset_prompts._DEFAULT_STYLE 完全一致，因此旧项目行为保持等价。
"""
from __future__ import annotations

import re
from typing import Any

# 配置结构版本：结构或枚举语义变更时递增，写入生成任务快照供追溯
STYLE_CONFIG_VERSION = "style-v1.0"

# 旧项目默认视觉风格（等价于历史硬编码 _DEFAULT_STYLE，不得随意替换）
DEFAULT_VISUAL_STYLE = "storybook_realism"

MAX_CUSTOM_LENGTH = 500

# ── 剧本类型（题材，可多选；第一个为主类型）────────────────
GENRE_GROUPS: dict[str, list[str]] = {
    "都市与情感": [
        "都市情感", "甜宠恋爱", "虐恋", "婚姻家庭",
        "家庭伦理", "豪门恩怨", "职场商战", "社会现实",
    ],
    "古装与历史": ["古装权谋", "宫廷争斗", "历史传奇", "武侠江湖", "民国传奇", "战争历史"],
    "奇幻与幻想": [
        "穿越", "重生", "玄幻", "仙侠", "系统流",
        "异世界", "神话传说", "魔法冒险", "科幻幻想",
    ],
    "悬疑与动作": [
        "悬疑推理", "刑侦犯罪", "复仇反转", "动作冒险",
        "谍战", "惊悚恐怖", "灾难求生",
    ],
    "青春与成长": ["校园青春", "友情成长", "励志奋斗", "亲子教育", "师生故事", "运动竞技"],
    "儿童与动画故事": [
        "童话故事", "寓言故事", "儿童冒险", "亲子故事",
        "校园儿童剧", "动物故事", "科普剧情", "儿童成长",
    ],
    "喜剧与生活": [
        "搞笑喜剧", "荒诞喜剧", "黑色幽默", "乡村生活",
        "职场日常", "家庭生活", "社会寓言",
    ],
}
GENRE_TAGS: list[str] = [tag for tags in GENRE_GROUPS.values() for tag in tags]

# ── 叙事风格（可多选；第一个为主风格）──────────────────────
NARRATIVE_STYLES: dict[str, dict[str, str]] = {
    "写实细腻型": {"constraint": "人物行为符合动机，细节自然，减少刻意煽情"},
    "戏剧冲突型": {"constraint": "尽早建立目标和障碍，突出人物之间的冲突"},
    "爽感逆袭型": {"constraint": "建立清晰的受挫、反击和结果反馈，不得无因反转"},
    "悬念反转型": {"constraint": "建立问题、线索和伏笔；反转必须能由前文信息支撑"},
    "轻松幽默型": {"constraint": "通过性格反差、误会和节奏制造笑点，避免无关搞笑"},
    "温情治愈型": {"constraint": "强调理解、成长、陪伴和情绪缓和"},
    "寓教于乐型": {"constraint": "将知识或价值观融入角色选择和故事结果，避免说教堆砌"},
    "诗意抒情型": {"constraint": "重视意象、氛围和情绪表达，但仍保证事件可理解"},
    "黑暗压抑型": {"constraint": "强化压力、不确定性和心理氛围；需符合目标受众限制"},
    "史诗宏大型": {"constraint": "强调世界背景、长期目标、群像与命运变化"},
}

# ── 视觉表现风格（单选，主风格唯一）─────────────────────
# prompt 字段即写入提示词的美术风格片段，必须自洽、不含未定义元素。
VISUAL_STYLES: dict[str, dict[str, str]] = {
    "storybook_realism": {
        "label": "童话绘本写实风",
        "summary": "童话绘本的柔和写实质感，色彩温和，光照温暖",
        "prompt": "童话绘本写实风格，柔和饱和色彩，温暖光照，细腻质感",
    },
    "cinematic_realism": {
        "label": "电影写实风",
        "summary": "真实材质与自然光影，可信比例，电影构图",
        "prompt": "电影写实风格，真实材质质感，自然光影，可信的人体与物体比例，电影级构图与景深，细腻环境细节",
    },
    "3d_cartoon": {
        "label": "3D 卡通动画风",
        "summary": "统一 3D 造型，简洁清晰轮廓，动画电影布光",
        "prompt": "3D 卡通动画风格，统一的立体造型，简洁清晰的轮廓，柔和材质，动画电影式布光",
    },
    "chinese_ink": {
        "label": "国风水墨风",
        "summary": "水墨笔触，东方构图，留白与墨色层次",
        "prompt": "国风水墨风格，水墨笔触与墨色层次，东方留白构图，写意环境氛围",
    },
    "anime_2d": {
        "label": "二次元动漫风",
        "summary": "风格化线条与动画色彩，角色设计统一",
        "prompt": "二次元动漫风格，风格化线条，动画式配色，清晰轮廓，统一的角色设计",
    },
    "storybook_watercolor": {
        "label": "绘本水彩风",
        "summary": "水彩晕染与纸张质感，柔和配色",
        "prompt": "绘本水彩风格，水彩晕染与纸张质感，柔和配色，绘本式构图",
    },
    "flat_illustration": {
        "label": "二维扁平插画风",
        "summary": "扁平色块与简化形状，有限阴影",
        "prompt": "二维扁平插画风格，扁平色块，清晰轮廓，简化形状，有限的层次阴影",
    },
    "comic": {
        "label": "漫画风",
        "summary": "漫画式造型与构图，单帧内不使用分格",
        "prompt": "漫画风格，漫画式造型与线稿结构，概括化的明暗关系；单帧画面内部保持完整构图",
    },
    "clay_stop_motion": {
        "label": "黏土定格风",
        "summary": "手工黏土质感与微缩布景",
        "prompt": "黏土定格动画风格，手工黏土材质质感，微缩布景，柔和灯光，统一的手工造型",
    },
    "low_poly": {
        "label": "低多边形风",
        "summary": "几何化造型与简洁体积关系",
        "prompt": "低多边形风格，几何化造型与棱面结构，简洁色块，清晰的体积关系",
    },
    "retro_film": {
        "label": "复古胶片风",
        "summary": "复古色调与胶片颗粒，年代感光影",
        "prompt": "复古胶片风格，复古色调，胶片颗粒质感，年代感光影；年代设定需与剧本一致",
    },
    "paper_cut": {
        "label": "剪纸风",
        "summary": "纸片层叠与平面构成",
        "prompt": "剪纸风格，纸片层叠结构，剪纸轮廓，平面构成，纸张材质质感",
    },
    "pixel_art": {
        "label": "像素风",
        "summary": "统一像素尺寸与有限色板",
        "prompt": "像素艺术风格，统一的像素尺寸，有限色板，清晰的像素边缘",
    },
}

# 旧配置中自由文本视觉风格的兼容别名（关键字匹配，命中即迁移到对应内部值）
_LEGACY_VISUAL_STYLE_ALIASES: list[tuple[tuple[str, ...], str]] = [
    (("水墨", "国风"), "chinese_ink"),
    (("3D", "三维"), "3d_cartoon"),
    (("水彩",), "storybook_watercolor"),
    (("二次元", "动漫"), "anime_2d"),
    (("漫画",), "comic"),
    (("黏土", "定格"), "clay_stop_motion"),
    (("低多边形", "lowpoly"), "low_poly"),
    (("胶片", "复古"), "retro_film"),
    (("剪纸",), "paper_cut"),
    (("像素",), "pixel_art"),
    (("扁平", "插画"), "flat_illustration"),
    (("真人", "实拍", "电影写实"), "cinematic_realism"),
    (("绘本", "童话"), "storybook_realism"),
]

# ── 剧情节奏（单选）─────────────────────────────────────
PACING: dict[str, dict[str, str]] = {
    "slow": {"label": "慢节奏", "summary": "保留观察、停顿和情绪变化",
             "prompt": "节奏舒缓，保留观察、停顿与情绪变化"},
    "gentle": {"label": "舒缓", "summary": "适合温情、绘本和低龄儿童故事",
               "prompt": "节奏轻柔平缓，适合温情与低龄叙事"},
    "balanced": {"label": "均衡", "summary": "交代、行动和情绪段落比例适中",
                 "prompt": "节奏均衡，交代、行动与情绪段落比例适中"},
    "tight": {"label": "紧凑", "summary": "减少重复信息，强化场景转折",
              "prompt": "节奏紧凑，减少重复信息，强化场景转折"},
    "fast": {"label": "快节奏", "summary": "快速进入目标或冲突，不导致因果断裂",
             "prompt": "节奏明快，快速进入目标与冲突，但保持因果完整"},
}

# ── 情绪基调（可多选；第一个为主情绪）───────────────────
EMOTIONAL_TONES: list[str] = [
    "欢乐", "温馨", "轻松", "紧张", "悲伤", "热血",
    "神秘", "治愈", "浪漫", "庄严", "黑暗压抑",
]

# ── 冲突强度（单选）────────────────────────────────────
CONFLICT_INTENSITY: dict[str, dict[str, str]] = {
    "low": {"label": "低", "prompt": "冲突以日常小问题、轻微误会或低风险选择为主"},
    "moderate": {"label": "中", "prompt": "冲突为明确障碍，需要人物主动行动，结果有一定影响"},
    "high": {"label": "高", "prompt": "冲突为重大危机或高风险选择，须符合目标受众与故事设定"},
}

# ── 反转密度（单选）────────────────────────────────────
TWIST_DENSITY: dict[str, dict[str, str]] = {
    "none": {"label": "不设计反转", "prompt": "不刻意设计反转"},
    "occasional": {"label": "少量反转", "prompt": "仅在关键节点安排少量且合理的反转"},
    "frequent": {"label": "多次反转", "prompt": "允许多次信息变化，但每次都必须能由前文解释"},
}

# ── 对白风格（单选）────────────────────────────────────
DIALOGUE_STYLES: dict[str, str] = {
    "natural": "生活化",
    "concise": "简洁直接",
    "humorous": "幽默风趣",
    "poetic": "诗意抒情",
    "dramatic": "戏剧化",
    "child_friendly": "儿童化、易理解",
}

# ── 叙述方式（单选）────────────────────────────────────
NARRATION_MODES: dict[str, str] = {
    "voiceover": "旁白驱动",
    "dialogue": "对白驱动",
    "action": "动作驱动",
    "mixed": "混合叙述",
}

# ── 目标受众（单选）────────────────────────────────────
TARGET_AUDIENCES: dict[str, dict[str, str]] = {
    "kindergarten_primary_2": {
        "label": "幼儿园至小学二年级",
        "prompt": "词汇简单、句子短、情节理解难度低，冲突与恐怖程度必须很低，镜头刺激度低，旁白充分解释",
    },
    "primary_middle_high": {
        "label": "小学中高年级",
        "prompt": "词汇适中、允许简单伏笔，冲突温和，避免强烈恐怖与血腥，镜头表达清晰",
    },
    "teen": {
        "label": "青少年",
        "prompt": "词汇与情节可含成长议题，允许中等冲突与悬念，避免极端暴力细节",
    },
    "adult": {
        "label": "成年观众",
        "prompt": "允许复杂动机、多层反转与较强冲突，但仍需因果自洽",
    },
    "all_ages": {
        "label": "全年龄段",
        "prompt": "兼顾理解门槛，冲突与恐怖程度控制在温和区间，避免过度刺激画面",
    },
}

# 低龄受众（用于冲突检测：黑暗/恐怖/高强度冲突需提示确认）
_CHILD_AUDIENCES = {"kindergarten_primary_2", "primary_middle_high"}

_VIOLENT_GENRES = {"惊悚恐怖", "灾难求生", "刑侦犯罪"}


# ── 默认配置 ─────────────────────────────────────────────
def default_style_config() -> dict:
    """新建项目的默认风格配置（最小必需字段 + 合理默认值）。"""
    return {
        "genre_tags": [],
        "narrative_styles": [],
        "visual_style": DEFAULT_VISUAL_STYLE,
        "pacing": "balanced",
        "emotional_tone": [],
        "conflict_intensity": "moderate",
        "twist_density": "occasional",
        "dialogue_style": "natural",
        "narration_mode": "mixed",
        "target_audience": "all_ages",
        "custom_style_requirements": "",
    }


STYLE_FIELDS: tuple[str, ...] = tuple(default_style_config().keys())


# ── 规范化工具 ────────────────────────────────────────────
def _split_tags(value: Any) -> list[str]:
    """把字符串/列表统一成保序去重的标签数组。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(v) for v in value]
    else:
        raw = re.split(r"[,，、;；/|]+", str(value))
    out: list[str] = []
    for item in raw:
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def _coerce_enum(options: dict, value: Any, default: str) -> str:
    """枚举值规范化：同时接受内部值与展示名（中文）。"""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate in options:
            return candidate
        for key, meta in options.items():
            label = meta.get("label") if isinstance(meta, dict) else meta
            if label and str(label) == candidate:
                return key
    return default


def _coerce_visual_style(value: Any) -> tuple[str, str | None]:
    """视觉风格规范化，返回 (内部值, 未识别时的原始文本)。"""
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_VISUAL_STYLE, None
    candidate = value.strip()
    if candidate in VISUAL_STYLES:
        return candidate, None
    for key, meta in VISUAL_STYLES.items():
        if meta["label"] == candidate:
            return key, None
    for keywords, key in _LEGACY_VISUAL_STYLE_ALIASES:
        if any(word in candidate for word in keywords):
            return key, None
    return DEFAULT_VISUAL_STYLE, candidate


def _clean_custom(text: Any) -> str:
    """自定义要求规则化：折叠空白、去换行、截断，避免污染提示词结构。"""
    if not isinstance(text, str) or not text.strip():
        return ""
    flat = re.sub(r"[\r\n\t]+", " ", text)
    flat = re.sub(r" {2,}", " ", flat).strip()
    return flat[:MAX_CUSTOM_LENGTH]


def normalize_style_config(raw: Any) -> dict:
    """把 Project.settings（含旧键）规范化为完整的风格配置字典。

    不抛异常：无法识别的取值回退默认，由 validate_style_config 负责报告问题。
    """
    source = dict(raw or {}) if isinstance(raw, dict) else {}
    cfg = default_style_config()

    # 题材：新字段优先，旧 genre 次之；未知标签保留（支持『其他/自定义』）
    genre = source.get("genre_tags")
    if genre in (None, "", []):
        genre = source.get("genre")
    tags = _split_tags(genre)
    if tags:
        cfg["genre_tags"] = tags

    # 叙事风格：过滤到已定义选项，未知项丢弃（由 validate 报告）
    narrative = [n for n in _split_tags(source.get("narrative_styles")) if n in NARRATIVE_STYLES]
    if narrative:
        cfg["narrative_styles"] = narrative

    # 视觉风格：单选，旧自由文本按别名迁移
    visual_style, _ = _coerce_visual_style(source.get("visual_style"))
    cfg["visual_style"] = visual_style

    cfg["pacing"] = _coerce_enum(PACING, source.get("pacing"), cfg["pacing"])

    tones = [t for t in _split_tags(source.get("emotional_tone")) if t in EMOTIONAL_TONES]
    if tones:
        cfg["emotional_tone"] = tones

    cfg["conflict_intensity"] = _coerce_enum(
        CONFLICT_INTENSITY, source.get("conflict_intensity"), cfg["conflict_intensity"])
    cfg["twist_density"] = _coerce_enum(TWIST_DENSITY, source.get("twist_density"), cfg["twist_density"])
    cfg["dialogue_style"] = _coerce_enum(DIALOGUE_STYLES, source.get("dialogue_style"), cfg["dialogue_style"])
    cfg["narration_mode"] = _coerce_enum(NARRATION_MODES, source.get("narration_mode"), cfg["narration_mode"])

    audience = source.get("target_audience") or source.get("audience")
    cfg["target_audience"] = _coerce_enum(TARGET_AUDIENCES, audience, cfg["target_audience"])

    custom = source.get("custom_style_requirements")
    if custom in (None, ""):
        custom = source.get("requirements")
    cfg["custom_style_requirements"] = _clean_custom(custom)

    return cfg


# ── 校验 ─────────────────────────────────────────────────
def _issue(code: str, message: str, severity: str = "warning") -> dict:
    return {"code": code, "message": message, "severity": severity}


# 单选枚举字段：(字段名, 取值表, 中文标签, 旧键别名)
# 用于识别旧自由文本或拼写错误导致的静默回退。
_ENUM_STYLE_FIELDS: tuple[tuple[str, dict, str, str | None], ...] = (
    ("pacing", PACING, "剧情节奏", None),
    ("conflict_intensity", CONFLICT_INTENSITY, "冲突强度", None),
    ("twist_density", TWIST_DENSITY, "反转密度", None),
    ("dialogue_style", DIALOGUE_STYLES, "对白风格", None),
    ("narration_mode", NARRATION_MODES, "叙述方式", None),
    ("target_audience", TARGET_AUDIENCES, "目标受众", "audience"),
)


def validate_style_config(raw: Any) -> tuple[dict, list[dict]]:
    """规范化并校验风格配置，返回 (配置, 问题列表)。

    问题不阻断保存：非法枚举纠正为默认并记录，超长自定义要求截断并标记 error。
    """
    source = dict(raw or {}) if isinstance(raw, dict) else {}
    issues: list[dict] = []

    if source.get("visual_style") not in (None, ""):
        _, unknown = _coerce_visual_style(source.get("visual_style"))
        if unknown:
            issues.append(_issue(
                "visual_style_unknown",
                f"视觉风格『{unknown}』不在支持列表中，已回退为默认风格"
                f"（{VISUAL_STYLES[DEFAULT_VISUAL_STYLE]['label']}）",
            ))

    for item in _split_tags(source.get("narrative_styles")):
        if item not in NARRATIVE_STYLES:
            issues.append(_issue("narrative_style_unknown", f"叙事风格『{item}』不在支持列表中，已忽略"))
    for item in _split_tags(source.get("emotional_tone")):
        if item not in EMOTIONAL_TONES:
            issues.append(_issue("emotional_tone_unknown", f"情绪基调『{item}』不在支持列表中，已忽略"))
    # 单选枚举字段：旧自由文本（如 dialogue_style=口语化）无法识别时须提示，
    # 否则会被 _coerce_enum 静默回退默认值而用户无感知。
    sentinel = object()
    for field, options, label, alias in _ENUM_STYLE_FIELDS:
        value = source.get(field)
        if value in (None, "") and alias:
            value = source.get(alias)
        if value in (None, ""):
            continue
        if _coerce_enum(options, value, sentinel) is sentinel:
            issues.append(_issue(
                f"{field}_unknown",
                f"{label}『{value}』不在支持列表中，已回退为默认值",
            ))

    custom_raw = source.get("custom_style_requirements") or source.get("requirements") or ""
    if isinstance(custom_raw, str) and len(custom_raw) > MAX_CUSTOM_LENGTH:
        issues.append(_issue(
            "custom_style_too_long",
            f"自定义要求超过 {MAX_CUSTOM_LENGTH} 字符，已截断",
            "error",
        ))

    cfg = normalize_style_config(source)

    child = cfg["target_audience"] in _CHILD_AUDIENCES
    if child and "黑暗压抑型" in cfg["narrative_styles"]:
        issues.append(_issue(
            "narrative_conflicts_audience",
            "叙事风格『黑暗压抑型』与低龄目标受众存在冲突，请确认是否保留",
        ))
    if child and cfg["conflict_intensity"] == "high":
        issues.append(_issue(
            "conflict_conflicts_audience",
            "冲突强度『高』超出低龄受众的推荐范围，请确认是否保留",
        ))
    if child and set(cfg["genre_tags"]) & _VIOLENT_GENRES:
        issues.append(_issue(
            "genre_conflicts_audience",
            "题材包含惊悚/犯罪/灾难类标签，与低龄目标受众存在冲突，请确认是否保留",
        ))
    if "黑暗压抑型" in cfg["narrative_styles"] and "温情治愈型" in cfg["narrative_styles"]:
        issues.append(_issue(
            "narrative_style_conflict",
            "同时选择了『黑暗压抑型』与『温情治愈型』，生成时按列表顺序取首个为主风格",
        ))
    return cfg, issues


# ── 提示词编译 ────────────────────────────────────────────
def compile_style_prompt(config: Any, *, task_type: str = "storyboard") -> str:
    """编译写入提示词的美术风格片段。

    - 资产图（character/scene/prop）：只给视觉风格与自定义要求，不掺入剧情情绪，
      保证资产图用于『定义身份』而不是『讲述剧情』；
    - 分镜图/分镜视频：视觉风格 + 节奏 + 主情绪，用于约束构图与光影氛围；
    - 剧本分析：只给视觉风格方向，不把美术描述写进剧情因果。
    """
    cfg = normalize_style_config(config)
    parts: list[str] = [VISUAL_STYLES[cfg["visual_style"]]["prompt"]]

    if task_type in ("storyboard", "shot_video"):
        parts.append(PACING[cfg["pacing"]]["prompt"])
        tones = cfg["emotional_tone"]
        if tones:
            main = tones[0]
            extra = "、".join(tones[1:3])
            parts.append(f"情绪基调以{main}为主" + (f"，辅以{extra}" if extra else ""))

    custom = cfg["custom_style_requirements"]
    if custom:
        parts.append(f"额外风格要求：{custom}")

    return "，".join(part for part in parts if part)


def style_snapshot(config: Any, *, project_id: str | None = None) -> dict:
    """生成不可变配置快照，写入生成任务 input_snapshot 供复现与排障。"""
    cfg = normalize_style_config(config)
    snap: dict[str, Any] = {"version": STYLE_CONFIG_VERSION}
    if project_id:
        snap["project_id"] = project_id
    snap.update({
        "genre_tags": list(cfg["genre_tags"]),
        "narrative_styles": list(cfg["narrative_styles"]),
        "visual_style": cfg["visual_style"],
        "visual_style_label": VISUAL_STYLES[cfg["visual_style"]]["label"],
        "visual_style_prompt": VISUAL_STYLES[cfg["visual_style"]]["prompt"],
        "pacing": cfg["pacing"],
        "emotional_tone": list(cfg["emotional_tone"]),
        "conflict_intensity": cfg["conflict_intensity"],
        "twist_density": cfg["twist_density"],
        "dialogue_style": cfg["dialogue_style"],
        "narration_mode": cfg["narration_mode"],
        "target_audience": cfg["target_audience"],
        "custom_style_requirements": cfg["custom_style_requirements"],
    })
    return snap


def describe_for_llm(config: Any) -> str:
    """把风格配置转成注入剧本分析提示词的中文约束段（剧本内容优先于风格修饰）。"""
    cfg = normalize_style_config(config)
    lines: list[str] = []

    if cfg["genre_tags"]:
        lines.append("剧本类型（第一个为主类型）：" + "、".join(cfg["genre_tags"]))
    if cfg["narrative_styles"]:
        main = cfg["narrative_styles"][0]
        extra = ("，辅助风格：" + "、".join(cfg["narrative_styles"][1:])) if len(cfg["narrative_styles"]) > 1 else ""
        lines.append(f"叙事风格：{main}为主{extra}")
        details = [
            f"{name}——{NARRATIVE_STYLES[name]['constraint']}"
            for name in cfg["narrative_styles"] if name in NARRATIVE_STYLES
        ]
        if details:
            lines.append("叙事约束：" + "；".join(details))
    lines.append(
        f"主视觉风格：{VISUAL_STYLES[cfg['visual_style']]['label']}"
        f"（{VISUAL_STYLES[cfg['visual_style']]['summary']}）"
    )
    lines.append(f"剧情节奏：{PACING[cfg['pacing']]['label']}——{PACING[cfg['pacing']]['summary']}")
    if cfg["emotional_tone"]:
        lines.append("情绪基调：" + "、".join(cfg["emotional_tone"]) + "（第一个为主情绪）")
    lines.append(
        f"冲突强度：{CONFLICT_INTENSITY[cfg['conflict_intensity']]['label']}"
        f"——{CONFLICT_INTENSITY[cfg['conflict_intensity']]['prompt']}"
    )
    lines.append(
        f"反转密度：{TWIST_DENSITY[cfg['twist_density']]['label']}"
        f"——{TWIST_DENSITY[cfg['twist_density']]['prompt']}"
    )
    lines.append(f"对白风格：{DIALOGUE_STYLES[cfg['dialogue_style']]}")
    lines.append(f"叙述方式：{NARRATION_MODES[cfg['narration_mode']]}")
    audience = TARGET_AUDIENCES[cfg["target_audience"]]
    lines.append(f"目标受众：{audience['label']}——{audience['prompt']}")
    if cfg["custom_style_requirements"]:
        lines.append(f"用户自定义风格要求：{cfg['custom_style_requirements']}")

    lines.append(
        "风格只约束表达方式、构图、光影、节奏与情绪，不得新增人物、道具、事件，"
        "不得改变故事因果与镜头绑定关系。"
    )
    return "\n".join(lines)


# ── 分析结果差异比对 ──────────────────────────────────────
def _normalize_tag(value: Any) -> str:
    """比对用归一化：去掉空白与常见标点，避免模型输出的格式漂移被判成不一致。"""
    text = str(value or "")
    return re.sub(r"[\s，,、;；:：。.·\-—_/|\"'“”‘’《》()（）\[\]【】]+", "", text).lower()


def compute_style_diff(config: Any, project_analysis: Any) -> list[dict]:
    """比对 LLM 在 project_analysis 中识别到的题材/叙事特征与用户配置。

    LLM 侧以 detected_genre_tags / detected_narrative_styles 输出；缺失时不报差异，
    不凭猜测补全，避免把『未识别』误判成『冲突』。
    """
    cfg = normalize_style_config(config)
    analysis = project_analysis if isinstance(project_analysis, dict) else {}
    diffs: list[dict] = []

    configured_genre_keys = {_normalize_tag(t) for t in cfg["genre_tags"]}
    detected_genre = _split_tags(analysis.get("detected_genre_tags"))
    # 用户未配置题材时不存在『不一致』，跳过以免把全量识别结果当成差异噪声
    if detected_genre and configured_genre_keys:
        missing = [t for t in detected_genre if _normalize_tag(t) not in configured_genre_keys]
        if missing:
            diffs.append({
                "field": "genre_tags",
                "message": "分析识别到未配置的题材标签：" + "、".join(missing),
                "detected": missing,
                "configured": list(cfg["genre_tags"]),
            })

    configured_narrative_keys = {_normalize_tag(n) for n in cfg["narrative_styles"]}
    detected_narrative = [
        n for n in _split_tags(analysis.get("detected_narrative_styles"))
        if n in NARRATIVE_STYLES
    ]
    # 同上：未配置叙事风格时不报差异
    if detected_narrative and configured_narrative_keys:
        missing = [n for n in detected_narrative if _normalize_tag(n) not in configured_narrative_keys]
        if missing:
            diffs.append({
                "field": "narrative_styles",
                "message": "分析识别到未配置的叙事风格：" + "、".join(missing),
                "detected": missing,
                "configured": list(cfg["narrative_styles"]),
            })

    return diffs


# ── 前端选项下发 ─────────────────────────────────────────
def options_payload() -> dict:
    """返回全部可选项（展示名 + 说明），供前端渲染配置面板，避免前端硬编码。"""
    return {
        "version": STYLE_CONFIG_VERSION,
        "genre_groups": [
            {"group": group, "tags": list(tags)} for group, tags in GENRE_GROUPS.items()
        ],
        "genre_tags": list(GENRE_TAGS),
        "narrative_styles": [
            {"value": key, "label": key, "description": meta["constraint"]}
            for key, meta in NARRATIVE_STYLES.items()
        ],
        "visual_styles": [
            {"value": key, "label": meta["label"], "description": meta["summary"]}
            for key, meta in VISUAL_STYLES.items()
        ],
        "pacing": [
            {"value": key, "label": meta["label"], "description": meta["summary"]}
            for key, meta in PACING.items()
        ],
        "emotional_tones": list(EMOTIONAL_TONES),
        "conflict_intensity": [
            {"value": key, "label": meta["label"], "description": meta["prompt"]}
            for key, meta in CONFLICT_INTENSITY.items()
        ],
        "twist_density": [
            {"value": key, "label": meta["label"], "description": meta["prompt"]}
            for key, meta in TWIST_DENSITY.items()
        ],
        "dialogue_styles": [
            {"value": key, "label": label} for key, label in DIALOGUE_STYLES.items()
        ],
        "narration_modes": [
            {"value": key, "label": label} for key, label in NARRATION_MODES.items()
        ],
        "target_audiences": [
            {"value": key, "label": meta["label"], "description": meta["prompt"]}
            for key, meta in TARGET_AUDIENCES.items()
        ],
        "max_custom_length": MAX_CUSTOM_LENGTH,
        "defaults": default_style_config(),
    }

# ── 剧本分析上下文段 ────────────────────────────────────────
def parse_context_block(config: Any) -> str:
    """构造注入剧本解析用户提示词的风格约束段。

    - 明确『剧本内容优先』：风格不得新增人物、道具、事件或改变因果；
    - 要求 image_prompt / video_prompt 的美术风格段使用项目主视觉风格原文；
    - 要求输出 detected_* 字段，供与用户配置做差异比对。
    """
    cfg = normalize_style_config(config)
    visual = VISUAL_STYLES[cfg["visual_style"]]["prompt"]
    return "\n".join([
        "【项目创作配置（优先于默认示例，但不得覆盖剧本事实与因果）】",
        describe_for_llm(cfg),
        f"生成 image_prompt 与 video_prompt 时，美术风格段必须使用以下主视觉风格原文：{visual}",
        "并在 project_analysis 中额外输出 detected_genre_tags（字符串数组，实际识别到的题材标签）"
        "与 detected_narrative_styles（字符串数组，实际识别到的叙事风格）；无法判断时输出空数组，不要臆测。",
        "",
    ])
