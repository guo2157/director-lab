"""阶段 B 提示词编译器（两阶段 LLM 设计中的 Prompt Compiler 落地实现）。

职责边界：
- 阶段 A（app.services.script_parse）只产出结构化事实，不产出最终生图/生视频提示词；
- 本模块把『结构化事实 + 模板 + 已审核参考图 + 工作流真实能力』编译成可校验的
  positive_prompt / negative_prompt / reference_slots / generation_constraints。

纪律（对应提示词规范）：
1. 模板集中管理：内置默认值注册到 prompt_store，可外置为 workspace/prompts/*.md 由 UI 编辑；
2. 输出必须通过 validate_compilation 校验后才允许提交生成任务，禁止静默丢弃字段；
3. 参考图槽位只能来自工作流 capabilities.reference_slots，超出能力即 blocking，不丢图；
4. 负向提示词 = 通用基础层 + 资产类型专用层，去重合并后一次下发；
5. 资产图与分镜图严格区分：资产图单一主体/干净背景，分镜图只组合镜头绑定的资产。
"""
from __future__ import annotations

import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Asset, Character, Job, Project, Prop, Scene, Shot, Workflow
from app.services import asset_prompts, style_config
from app.services.prompt_store import _register_default, get_prompt

# ── 任务类型与模板标识（写入 job.input_snapshot 供追溯）──────────
TASK_ASSET_IMAGE = "asset_image"
TASK_STORYBOARD_IMAGE = "storyboard_image"
TASK_SHOT_VIDEO = "shot_video"

TEMPLATE_CHARACTER = "character_asset_prompt"
TEMPLATE_SCENE = "scene_asset_prompt"
TEMPLATE_PROP = "prop_asset_prompt"
TEMPLATE_STORYBOARD = "storyboard_image_prompt"
TEMPLATE_VIDEO = "shot_video_prompt"

TEMPLATE_REGISTRY: dict[str, dict] = {
    TEMPLATE_CHARACTER: {"version": 1, "task_type": TASK_ASSET_IMAGE, "asset_type": "character",
                         "prompt_name": "compile_character_asset"},
    TEMPLATE_SCENE: {"version": 2, "task_type": TASK_ASSET_IMAGE, "asset_type": "scene",
                     "prompt_name": "compile_scene_asset"},
    TEMPLATE_PROP: {"version": 1, "task_type": TASK_ASSET_IMAGE, "asset_type": "prop",
                    "prompt_name": "compile_prop_asset"},
    TEMPLATE_STORYBOARD: {"version": 2, "task_type": TASK_STORYBOARD_IMAGE, "asset_type": "keyframe",
                          "prompt_name": "compile_storyboard_image"},
    TEMPLATE_VIDEO: {"version": 2, "task_type": TASK_SHOT_VIDEO, "asset_type": "clip",
                     "prompt_name": "compile_shot_video"},
}

ASSET_TEMPLATE_BY_TYPE = {
    "character": TEMPLATE_CHARACTER,
    "scene": TEMPLATE_SCENE,
    "prop": TEMPLATE_PROP,
}

# ── 通用负向基础层（与 docs/prompts/negative_common.md 保持一致）──────────
COMMON_NEGATIVE = (
    "低质量，低分辨率，模糊，失焦，严重噪点，压缩伪影，过曝，欠曝，颜色异常，结构错误，"
    "比例错误，透视错误，主体畸变，轮廓混乱，边缘破碎，重复主体，主体数量错误，元素融合，"
    "物体穿模，未经要求的额外元素，画面拥挤，杂乱背景，文字，乱码，字幕，标志，水印，"
    "签名，边框，拼贴，多格画面，九宫格，参考图缩略图，非预期的图形叠加"
)

# ── 资产类型专用负向层 ──────────────────────────────────────────
CHARACTER_NEGATIVE = (
    "多个角色，多个人物，群像，角色分身，额外人物，额外动物，手持无关道具，未定义配件，"
    "复杂场景背景，室内布景，室外风景，家具堆叠，背景故事元素，角色被裁切，缺失头部，"
    "缺失脚部，只显示半身，极端透视，遮挡主体，身体结构错误，额外手臂，额外腿，手指畸形，"
    "面部错位，五官重复，肢体融合，服装颜色错误，服装款式改变，头发颜色改变，比例漂移，"
    "角色设定表，多视图合集，身份漂移，面孔不一致，服装配饰不一致，年龄感不一致，"
    "配饰缺失或新增"
)
SCENE_NEGATIVE = (
    "角色，人物，群众，动物，人物剪影，人物倒影，角色海报，手，身体局部，剧情表演，"
    "未定义的关键道具，额外场景，多个地点拼接，分屏，空间结构混乱，地面与墙面融合，"
    "建筑结构错误，重复家具，杂乱堆放，过多装饰，未经定义的文字标牌，空间结构漂移，"
    "透视漂移，光线方向矛盾，阴影方向不一致，地平线倾斜"
)
PROP_NEGATIVE = (
    "多个相同道具，多件道具合集，道具堆叠，其他道具，人物，角色，手，手臂，身体，"
    "角色正在使用道具，场景背景，桌面杂物，包装盒，标签，说明文字，尺寸标尺，"
    "未定义配件，形状改变，颜色错误，材质错误，比例错误，结构缺失，主体被裁切，遮挡，"
    "重复部件，悬浮部件，结构融合，材质漂移，外形漂移，表面纹理不一致，"
    "部件数量增减，使用痕迹臆造"
)
STORYBOARD_NEGATIVE = (
    "未绑定角色，额外人物，额外动物，重复角色，角色分身，人物数量错误，错误服装，"
    "发型改变，脸型改变，体型漂移，角色颜色漂移，额外道具，错误道具，重复道具，"
    "道具形状改变，场景不匹配，场景结构改变，错误时间天气，角色与道具融合，"
    "角色穿过物体，额外肢体，肢体缺失，手部畸形，面部错位，目光方向错误，"
    "角色位置关系错误，动作含义错误，主体被遮挡，空间关系混乱，图中图，"
    "多画面拼贴，漫画格，分镜表，提示词文字，资产 ID，文件路径"
)
VIDEO_NEGATIVE = (
    "角色身份变化，脸部变化，服装变化，发型变化，角色数量变化，角色复制，角色消失，"
    "角色瞬移，场景跳变，背景结构改变，道具消失，道具变形，道具复制，道具颜色改变，"
    "动作断裂，动作反向，动作循环错误，时间顺序错误，肢体融合，身体扭曲，面部闪烁，"
    "纹理闪烁，亮度闪烁，画面抖动，镜头无故切换，突兀变焦，非预期转场，透视跳变"
)

_NEGATIVE_BY_TYPE = {
    "character": CHARACTER_NEGATIVE,
    "scene": SCENE_NEGATIVE,
    "prop": PROP_NEGATIVE,
    "keyframe": STORYBOARD_NEGATIVE,
    "clip": VIDEO_NEGATIVE,
}

_LEGACY_NEGATIVE = {
    "character": asset_prompts.CHARACTER_NEGATIVE,
    "scene": asset_prompts.SCENE_NEGATIVE,
    "prop": asset_prompts.PROP_NEGATIVE,
}

# ── 提示词模板（外置文件缺失时使用；docs/prompts 下保持同名同内容）──────
CHARACTER_TEMPLATE = """生成一张独立的角色设定参考图，画面中只能有一个主体：{name}。
角色外观必须严格遵循：
- 类型或物种：{species}
- 年龄观感：{age_appearance}
- 脸部与头部特征：{face_and_head}
- 发型或毛发：{hair_or_head}
- 体型与身材：{body_shape}
- 服装：{clothing}
- 固定配件：{fixed_accessories}
- 主色与材质：{main_colors}
- 必须保留的辨识特征：{distinctive_features}
构图要求：单角色、全身完整入画、自然稳定站姿、正面或轻微四分之三角度、头部到脚部均清晰可见、主体居中、轮廓清楚，可直接作为后续分镜的角色参考图。
背景要求：干净统一的纯色或低干扰背景，不展示故事发生的场景。
一致性要求：不自行改变上述外观，不增加剧本未定义的服装、配件、图案或身体特征；除明确列出的固定配件外，角色不手持道具。
画面限制：不生成角色设定表、四视图、表情合集或多角色拼图。
画面风格：{style}"""

SCENE_TEMPLATE = """场景板（空镜）：{name}，{time_and_weather}，{empty_expr}，只有景色与建筑。
前景：{foreground}，地面材质与近处结构真实清晰，地平面必须水平。
中景：{midground}。中景必须保留一段可见的干净地面与纵深作为后续人物合成的站位区，接触阴影有处可落，人物有立足点。
远景：{background}，层次分明，有雾气或纵深过渡。
光线：{lighting}，光源方向单一且写死，地面反光、玻璃反光与阴影长度全部按此方向一致；画面只允许一个投影方向。
镜头：{camera_shot}，前中后景层次清晰。
质感：{texture}，主色调与材质：{palette}。
环境定义：{identity}。允许出现的固定环境元素：{fixed_elements}。不得出现的剧情道具、车辆与可移动杂物：{excluded_props}。
不出现人物、行人、车辆或动物；不出现任何文字、招牌、路牌、海报或价目表（需要门头时写纯色无字招牌）；整个画面只由环境本身与天空构成。
画面风格：{style}"""

PROP_TEMPLATE = """生成一张独立的道具参考图，画面中只能有一件道具：{name}。
道具定义：{identity}
形状与结构：{shape}
颜色：{color}
材质与表面细节：{material}
相对尺寸与比例：{relative_scale}
必须保留的关键细节：{distinctive_features}
构图要求：整件道具完整入画、主体居中、轮廓清晰、形状容易辨认，采用干净统一的纯色或低干扰背景；以独立物品展示，不放进故事场景，不表现使用过程。
一致性要求：不增加数量、装饰、图案、附件或包装；同一件道具的多部件结构整体展示，不拆成多件独立物品。
画面风格：{style}"""

STORYBOARD_TEMPLATE = """生成镜头 {shot_id} 的单帧分镜参考图，表现当前镜头的具体瞬间，不是角色设定图、场景设定图或多格分镜表。
编辑目标：{scene_target}。构图、透视与光线方向必须与编辑底图严格一致。
画面主体：只出现本镜头绑定的角色 {characters}；场景为 {scene}；允许出现的道具 {props}。
参考绑定：{ref_bindings}。角色身份只由对应参考图锁定，提示词不重复描述五官细节；所有出现的人脸、体型、服装、配色保持与对应参考图完全一致，各角色独立保持身份。
动作与叙事瞬间：{action}。
角色相对位置与视线：{positions}。
道具与角色或环境的关系：{prop_interaction}。
画面情绪：{emotion}。
镜头语言：景别 {shot_size}；机位与角度 {camera_angle}；构图 {composition}；运动暗示 {camera_movement}；光线与色彩 {lighting}。
画面限制：只生成一张完整单帧画面，参考图仅作为身份与结构来源。
画面风格：{style}"""

VIDEO_TEMPLATE = """integrated_multimodal_description:
Scene: {scene}, with characters {characters} and props {props}. Overall style: {style}. This video must continue the approved storyboard reference image: keep character identity, face, body, costume, colors, props and scene structure exactly consistent in every frame.

[Shot 1] {main_action}. Start state: {start_state}. End state: {end_state}. Shot size: {shot_size}. Camera: {camera_angle}. Movement: {camera_movement}. Pacing: {pacing}. Environment changes: {environment_changes}. Continuity in: {continuity_in_type}; source shot: {source_shot_id}; source frame: {source_frame_file_id}. Continuity out: {continuity_out_state}. Dialogue or narration: {dialogue}.

overall_soundscape: {soundscape}

non_diegetic_music: {music}"""

_register_default("compile_character_asset", CHARACTER_TEMPLATE)
_register_default("compile_scene_asset", SCENE_TEMPLATE)
_register_default("compile_prop_asset", PROP_TEMPLATE)
_register_default("compile_storyboard_image", STORYBOARD_TEMPLATE)
_register_default("compile_shot_video", VIDEO_TEMPLATE)


# ── 编译结果 ────────────────────────────────────────────────
@dataclass
class CompiledPrompt:
    task_type: str
    template_id: str
    template_version: int
    entity_id: str
    asset_type: str | None
    positive_prompt: str
    negative_prompt: str
    reference_slots: list[dict] = field(default_factory=list)
    generation_constraints: dict = field(default_factory=dict)
    blocking_issues: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    canonical_snapshot: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "needs_review" if self.blocking_issues else "ready"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "task_type": self.task_type,
            "asset_type": self.asset_type,
            "entity_id": self.entity_id,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "reference_slots": self.reference_slots,
            "generation_constraints": self.generation_constraints,
            "blocking_issues": self.blocking_issues,
            "warnings": self.warnings,
            "canonical_snapshot": self.canonical_snapshot,
        }


# ── 基础工具 ────────────────────────────────────────────────
def _as_dict(value: Any) -> dict:
    """把结构化文本字段安全解析为 dict（Text 列可能存 JSON 字符串）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _issue(code: str, message: str, **extra) -> dict:
    out = {"code": code, "message": message}
    out.update(extra)
    return out


# 未提供占位与空值行：渲染后整行移除，避免占位文本稀释纯净约束的权重
_EMPTY_FIELD_MARKERS = ("未提供", "", "无")


def _prune_empty_fields(prompt: str) -> str:
    """删除『- 字段：未提供』这类空字段行，让提示词只保留真实生效的约束。

    缺失字段不再以占位形式进入模型，避免大量无信息文本挤占注意力权重；
    同时保留『必须保留的辨识特征』等已有兜底的字段不动。
    """
    kept: list[str] = []
    for line in (prompt or "").split("\n"):
        stripped = line.strip().lstrip("-*• ").strip()
        if "：" in stripped:
            value = stripped.split("：", 1)[1].strip()
            if value in _EMPTY_FIELD_MARKERS:
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def _text(value: Any, fallback: str = "未提供") -> str:
    if value is None:
        return fallback
    if isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "、".join(items) if items else fallback
    if isinstance(value, dict):
        items = [str(v).strip() for v in value.values() if str(v).strip()]
        return "、".join(items) if items else fallback
    text = str(value).strip()
    return text or fallback


def merge_negative(*parts: str) -> str:
    """合并负向提示词：按中英文分隔符切分后去重，保持首次出现顺序。"""
    seen: list[str] = []
    for part in parts:
        for term in re.split(r"[，,、;；]\s*", part or ""):
            item = term.strip()
            if item and item not in seen:
                seen.append(item)
    return "，".join(seen)


def negative_for(asset_or_task_type: str) -> str:
    """返回『通用基础层 + 类型专用层（含历史 P1 负向词）』合并后的负向提示词。"""
    specific = _NEGATIVE_BY_TYPE.get(asset_or_task_type, "")
    legacy = _LEGACY_NEGATIVE.get(asset_or_task_type, "")
    return merge_negative(COMMON_NEGATIVE, specific, legacy)


def render_template(template_id: str, mapping: dict) -> str:
    """渲染集中管理的模板；缺字段时显式报错，不输出半成品提示词。

    占位符完整性只在『模板侧』校验：解析出模板自身的字段名并核对 mapping 是否覆盖。
    不对渲染结果做花括号探测——用户数据（角色名、道具名、风格描述）本身可能含 { }，
    那是合法内容，不应被当成模板残留占位符而阻断生成。
    """
    meta = TEMPLATE_REGISTRY.get(template_id)
    if meta is None:
        raise ValueError(f"unknown_template: {template_id}")
    template = get_prompt(meta["prompt_name"])
    missing: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name and field_name not in mapping:
            missing.append(field_name)
    if missing:
        raise ValueError(
            f"prompt_template_missing_field: {template_id}: {sorted(set(missing))}"
        )
    try:
        rendered = template.format(**mapping)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"prompt_template_missing_field: {template_id}: {exc}") from exc
    return rendered.strip()


def _media_file_exists(file_path: str | None) -> bool:
    """兼容三种存储口径：绝对路径、媒体目录相对路径、backend 根相对路径（data/media/...）。

    历史/存量资产的 file_path 可能带 data/media/ 前缀（相对 backend 根目录），
    直接与 media_dir 拼接会得到 data/media/data/media/... 导致误判不存在。
    """
    if not file_path:
        return False
    path = Path(file_path)
    if path.is_absolute():
        return path.exists()
    media_dir = Path(get_settings().media_dir)
    if (media_dir / file_path).exists():
        return True
    # file_path 已含 media_dir 前缀（相对 backend 根）：先剥前缀再拼，或直接按原路径判断
    normalized = file_path.replace("\\", "/")
    prefix = str(media_dir).replace("\\", "/").rstrip("/") + "/"
    if normalized.startswith(prefix):
        return (media_dir / normalized[len(prefix):]).exists()
    return path.exists()


def _capabilities(workflow: Workflow | None) -> dict:
    return dict(workflow.capabilities or {}) if workflow is not None else {}


def _project_for(db: Session, entity: Any) -> Project | None:
    # 按实体所属项目读取项目配置；实体无 project_id 时返回 None
    project_id = getattr(entity, "project_id", None)
    if not project_id:
        return None
    return db.get(Project, project_id)


def _style_for(db: Session, project: Project | None, task_type: str) -> str:
    # 编译本任务使用的美术风格片段：项目主视觉风格是唯一来源（一个项目只允许一个主视觉风格）；
    # 项目缺失或旧项目无新字段时回退历史默认风格，保证旧结果风格不变。
    if project is None:
        return asset_prompts._DEFAULT_STYLE
    try:
        return style_config.compile_style_prompt(project.settings or {}, task_type=task_type)
    except Exception:  # noqa: BLE001
        return asset_prompts._DEFAULT_STYLE


def _declared_slots(capabilities: dict) -> list[dict]:
    """归一化工作流 reference_slots：兼容字符串数组与对象数组两种声明。"""
    slots: list[dict] = []
    for item in capabilities.get("reference_slots") or []:
        if isinstance(item, dict):
            param = item.get("param")
            input_key = item.get("input_key")
            required = not bool(item.get("optional", True))
        else:
            param, input_key, required = item, None, False
        if param:
            slots.append({"param": param, "input_key": input_key, "required": required})
    return slots


def _slot_limit(capabilities: dict, declared: list[dict]) -> int:
    limit = capabilities.get("max_reference_images")
    if isinstance(limit, int) and limit > 0:
        return limit
    return len(declared)


def _approved_asset(db: Session, asset_id: str | None, project_id: str,
                    expected_types: tuple[str, ...]) -> tuple[Asset | None, dict | None]:
    """校验参考图资产：存在、属于本项目、类型正确、current 且文件真实可读。"""
    if not asset_id:
        return None, None
    asset = db.get(Asset, asset_id)
    if asset is None:
        return None, _issue("reference_asset_missing", "参考图资产不存在", asset_id=asset_id)
    if asset.project_id != project_id:
        return None, _issue("reference_asset_foreign", "参考图资产不属于当前项目", asset_id=asset_id)
    if expected_types and asset.asset_type not in expected_types:
        return None, _issue("reference_asset_type_mismatch", "参考图资产类型与槽位不匹配",
                            asset_id=asset_id, asset_type=asset.asset_type)
    if asset.validity != "current":
        return None, _issue("reference_asset_not_current", "参考图资产不是当前有效版本",
                            asset_id=asset_id, validity=asset.validity)
    if not _media_file_exists(asset.file_path):
        return None, _issue("reference_asset_file_missing", "参考图文件不存在或不可读",
                            asset_id=asset_id, file_path=asset.file_path)
    return asset, None


def _resolve_workflow(db: Session, target: str, workflow_id: str | None) -> Workflow | None:
    """按 target 解析工作流；预览场景（无可用工作流）返回 None 以便展示无槽位能力。"""
    from app.api.v1.generate import _match_workflow
    try:
        return _match_workflow(db, target, workflow_id)
    except Exception:  # noqa: BLE001
        return None


# ── 资产图编译 ───────────────────────────────────────────────
def _character_mapping(character: Character, style: str | None = None) -> dict:
    appearance = character.appearance or character.description or ""
    traits = _as_dict(character.traits)
    return {
        "name": character.name or "未命名角色",
        "species": _text(traits.get("species_or_type")),
        "age_appearance": _text(traits.get("age_appearance")),
        "face_and_head": _text(traits.get("face")),
        "hair_or_head": _text(traits.get("hair_or_head")),
        "body_shape": _text(traits.get("body_shape")),
        "clothing": _text(traits.get("clothing")),
        "fixed_accessories": _text(traits.get("fixed_accessories"), "无"),
        "main_colors": _text(traits.get("main_colors")),
        "distinctive_features": _text(traits.get("distinctive_features"), appearance or "按角色定义"),
        "style": style or _text(traits.get("style"), asset_prompts._DEFAULT_STYLE),
    }


import re

def _strip_story_content(text: str | None) -> str:
    """清洗场景描述：从剧本解析产出的混合文本中剔除剧情内容，仅保留环境要素。
    处理：按从句切分后过滤含角色/动作谓语的从句，将生物提及降维为环境痕迹，
    保留地形名词、时间状语、天气与光照信息，不臆造新内容。
    """
    if not text:
        return ""
    # 生物提及 → 环境痕迹降维（cfg=1 下负向词无效，只能靠正向改写消除主体）
    trace_map = {
        r"羊群[^，。；]*吃草": "被啃短的草皮",
        r"(羊|羊群)[^，。；]*": "草地上零星的牲畜痕迹",
        r"(狼|灰狼)[^，。；]*走出": "被压倒的高草与向林中延伸的窄径",
        r"(狼|灰狼)[^，。；]*": "林缘被压倒的草痕",
    }
    # 剧情类谓语（动作/对话/情节），命中即整句丢弃
    story_verbs = (
        "呼救|喊|叫|哭|笑|跑|奔|赶|扑|追|道歉|承认|拒绝|警告|劝|说|喊道|回答|"
        "发呆|无聊|戏弄|恶作剧|惊|四散|旁白|总结|点明"
    )
    # 通用角色指称（含本项目角色名与泛称）
    char_pattern = (
        "放羊娃|牧童|男孩|少年|村民|农夫|大灰狼|灰狼|Shepherd|Boy|Villager|Wolf|"
        "角色|人物|男孩们|孩子们"
    )
    clauses = re.split(r"([，。；])", text)
    kept: list[str] = []
    buf = ""
    for part in clauses:
        if part in ("，", "。", "；"):
            buf += part
            continue
        seg = buf + part
        buf = ""
        # 先做生物→痕迹降维，再判断是否仍含角色/动作
        for pat, rep in trace_map.items():
            seg = re.sub(pat, rep, seg)
        if re.search(char_pattern, seg) or re.search(story_verbs, seg):
            continue
        kept.append(seg)
    removed = "".join(kept)
    # 清理多余标点与空白
    removed = re.sub(r"[,，、]{2,}", "，", removed)
    removed = re.sub(r"^[,，、。；]+|[,，、。；]+$", "", removed)
    removed = removed.strip()
    # 极端清洗结果太短时回退到纯空间描述（避免整个 identity 为空）
    return removed if len(removed) >= 3 else (text.split("，")[0].split("。")[0] if text else "")


def _scene_mapping(scene: Scene, style: str | None = None) -> dict:
    # 场景板（plate）优先：LLM 编译器产出的空镜规格（P1），规则清洗为兜底
    if scene.plate_text:
        # 取前三行（时间/环境穷举、光线、镜头），第四行是风格——模板已单独注入 {style}，避免重复
        plate_lines = [l for l in scene.plate_text.splitlines() if l.strip()][:3]
        identity = "\n".join(plate_lines)
        plate_name = None
        if scene.plate:
            try:
                import json as _json
                _plate = _json.loads(scene.plate) if isinstance(scene.plate, str) else scene.plate
                plate_name = _plate.get("location_name") if isinstance(_plate, dict) else None
            except (ValueError, TypeError):
                plate_name = None
        return {
            "name": plate_name or scene.title or scene.description or "未命名场景",
            "identity": identity,
            "spatial_layout": identity,
            "time_and_weather": _text(scene.time_note) or "白日",
            "lighting": "按场景板定义的单一自然光源，光向与投影方向全部一致",
            "palette": "按场景板定义的主色调",
            "fixed_elements": "场景板穷举的固定环境元素",
            "excluded_props": "剧情关键道具、可移动手持物品、临时摆放物件、车辆与杂物",
            "empty_expr": f"空无一人、荒无人烟，整个画面只由场景板穷举的固定环境元素与天空构成",
            "foreground": "按场景板前景定义",
            "midground": "按场景板中景定义，保留站位区",
            "background": "按场景板远景定义",
            "camera_shot": "24mm 广角，平视机位，一点透视，深景深全部清晰",
            "texture": "自然真实材质质感",
            "style": style or asset_prompts._DEFAULT_STYLE,
        }
    clean_desc = _strip_story_content(scene.description)
    time_weather = _text(scene.time_note)
    identity = clean_desc or time_weather or "室外自然环境"
    empty_expr = (
        f"空无一人、荒无人烟，整个画面只由{identity}中的固定环境元素与天空构成"
    )
    return {
        "name": scene.title or scene.description or "未命名场景",
        "identity": identity,
        "spatial_layout": clean_desc or _text(scene.description),
        "time_and_weather": time_weather or "白日",
        "lighting": "按场景定义的单一自然光源，写明光线入射方向与色温，地面反光与阴影方向全部一致",
        "palette": "按场景定义的主色调",
        "fixed_elements": "场景固有的地形、植被、建筑与固定布景",
        "excluded_props": "剧情关键道具、可移动手持物品、临时摆放物件、车辆与杂物",
        "empty_expr": empty_expr,
        "foreground": "按场景定义的地面材质与近处结构（如沙滩、积水、路面、近处墙体），保留自然反光",
        "midground": "主体建筑或地形，明确高度与材质",
        "background": "天际线或远景轮廓，薄雾层次，纵深清晰",
        "camera_shot": "24mm 广角，平视机位，一点透视，深景深全部清晰",
        "texture": "电影感写实摄影，轻微胶片颗粒，高动态范围",
        "style": style or asset_prompts._DEFAULT_STYLE,
    }


def _prop_mapping(prop: Prop, style: str | None = None) -> dict:
    data = _as_dict(prop.appearance)
    appearance = prop.description or ""
    return {
        "name": prop.name or "未命名道具",
        "identity": _text(prop.appearance if isinstance(prop.appearance, str) else None, appearance or "按道具定义"),
        "shape": _text(data.get("shape")),
        "color": _text(data.get("color")),
        "material": _text(data.get("material")),
        "relative_scale": _text(data.get("relative_scale")),
        "distinctive_features": _text(data.get("distinctive_features"), appearance or "按道具定义"),
        "style": style or asset_prompts._DEFAULT_STYLE,
    }


def compile_asset_image(db: Session, asset_type: str, entity_id: str, *,
                        workflow: Workflow | None = None) -> CompiledPrompt:
    """编译角色/场景/道具独立资产图提示词（单主体、干净背景、类型隔离）。"""
    template_id = ASSET_TEMPLATE_BY_TYPE.get(asset_type)
    if template_id is None:
        raise ValueError(f"unsupported_asset_type: {asset_type}")

    if asset_type == "character":
        entity = db.get(Character, entity_id)
    elif asset_type == "scene":
        entity = db.get(Scene, entity_id)
    else:
        entity = db.get(Prop, entity_id)
    if entity is None:
        raise LookupError(entity_id)

    project = _project_for(db, entity)
    style = _style_for(db, project, asset_type)
    style_snap = style_config.style_snapshot((project.settings if project else {}) or {},
                                             project_id=project.id if project else None)
    if asset_type == "character":
        mapping = _character_mapping(entity, style)
    elif asset_type == "scene":
        mapping = _scene_mapping(entity, style)
    else:
        mapping = _prop_mapping(entity, style)

    meta = TEMPLATE_REGISTRY[template_id]
    blocking: list[dict] = []
    warnings: list[str] = []

    # 身份兜底（沿用既有资产模板语义）：外观/描述优先，缺失时以名称兜底，不虚构外观；
    # 名称也为空才算真正缺少身份，此时阻断。
    name_source = getattr(entity, "name", None) or getattr(entity, "title", None) or ""
    identity_source = getattr(entity, "appearance", None) or getattr(entity, "description", None)
    if isinstance(identity_source, dict):
        identity_source = " ".join(str(v) for v in identity_source.values() if v)
    if not str(identity_source or "").strip():
        if str(name_source).strip():
            warnings.append(
                f"asset_identity_fallback_to_name: {asset_type} 缺少外观或描述，"
                "仅以名称为身份渲染，不虚构外观细节"
            )
        else:
            blocking.append(_issue("asset_identity_missing",
                                   f"{asset_type} 缺少名称与外观，无法编译资产图提示词",
                                   entity_id=entity_id))

    if workflow is not None:
        from app.api.v1.generate import _workflow_binds_param
        if not _workflow_binds_param(workflow, "negative"):
            warnings.append(
                "negative_prompt_unsupported: 当前工作流未绑定 negative 参数，负向提示词仅存档"
            )

    constraints = {
        "isolation_rule": {
            "character": "single_character_only",
            "scene": "environment_only_no_characters",
            "prop": "single_prop_only_no_hands",
        }[asset_type],
        "background_rule": "plain_neutral_background",
        "must_not_add": (
            ["other characters", "story scene", "text", "watermark", "collage"]
            if asset_type == "character" else
            (["characters", "people", "animals", "text", "watermark", "collage"]
             if asset_type == "scene" else
             ["people", "hands", "other props", "story scene", "text", "watermark"])
        ),
        "workflow_id": workflow.id if workflow is not None else None,
    }

    return CompiledPrompt(
        task_type=TASK_ASSET_IMAGE,
        template_id=template_id,
        template_version=int(meta["version"]),
        entity_id=entity_id,
        asset_type=asset_type,
        positive_prompt=_prune_empty_fields(render_template(template_id, mapping)),
        negative_prompt=negative_for(asset_type),
        reference_slots=[],
        generation_constraints={**constraints, "style_config": style_snap},
        blocking_issues=blocking,
        warnings=warnings,
        canonical_snapshot={
            "asset_type": asset_type,
            "entity_id": entity_id,
            "name": getattr(entity, "name", None) or getattr(entity, "title", None),
            "mapping": {k: v for k, v in mapping.items() if k != "style"},
            "style": style_snap,
            "template_id": template_id,
            "template_version": int(meta["version"]),
        },
    )


# ── 分镜图编译 ───────────────────────────────────────────────
def _shot_characters(db: Session, shot: Shot) -> list[Character]:
    """镜头出场角色：主角 + 对白分段说话人（兼容 speaker_id 与 speaker 名字两种存法）。

    多参考图工作流（4 个角色槽）依赖这里拿到全部出场角色，
    否则对白里出现第二个角色时，媒体注入了它、提示词却没绑定它。
    """
    ordered: list[Character] = []
    seen_ids: set[str] = set()

    def _add(ch: Character | None) -> None:
        if ch is not None and ch.id not in seen_ids:
            seen_ids.add(ch.id)
            ordered.append(ch)

    if shot.character_id:
        _add(db.get(Character, shot.character_id))
    scene = db.get(Scene, shot.scene_id)
    project_id = scene.project_id if scene is not None else None
    for seg in (shot.dialogue_segments or []):
        if not isinstance(seg, dict):
            continue
        speaker_id = seg.get("speaker_id") or seg.get("character_id")
        if speaker_id:
            _add(db.get(Character, speaker_id))
            continue
        name = seg.get("speaker")
        if name:
            query = db.query(Character).filter(Character.name == name)
            if project_id:
                query = query.filter(Character.project_id == project_id)
            _add(query.first())
    return ordered


def _shot_props(db: Session, shot: Shot, project_id: str) -> list[Prop]:
    from app.services.ref_inject import extract_refs
    try:
        names = set(extract_refs(shot.image_prompt or ""))
    except Exception:  # noqa: BLE001
        names = set()
    if not names or not project_id:
        return []
    rows = db.query(Prop).filter(Prop.project_id == project_id).all()
    return [p for p in rows if p.name in names]


def _review_gate(project: Project | None) -> dict | None:
    if project is None:
        return _issue("project_not_found", "项目不存在")
    if project.assets_review_status != "approved":
        return _issue("assets_review_pending",
                      "公共资产尚未审核通过，禁止生成分镜图",
                      assets_review_status=project.assets_review_status)
    return None


def _append_slot(slots: list[dict], declared_param_map: dict, param: str, *,
                 entity_id: str, entity_type: str, asset: Asset, role: str,
                 blocking: list[dict], asset_label: str) -> None:
    declared = declared_param_map.get(param)
    if declared is None:
        blocking.append(_issue(
            "reference_capacity_exceeded",
            f"当前工作流不支持 {asset_label} 参考槽位 {param}，请减少引用或配置支持该槽位的适配器",
            entity_id=entity_id, slot_key=param,
        ))
        return
    slots.append({
        "slot_key": param,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "approved_asset_file_id": asset.id,
        "file_path": asset.file_path,
        "role": role,
        "required": bool(declared.get("required")),
        "input_key": declared.get("input_key"),
    })


def _assert_unique_slots(slots: list[dict], blocking: list[dict]) -> None:
    seen: set[str] = set()
    for slot in slots:
        key = slot["slot_key"]
        if key in seen:
            blocking.append(_issue("reference_slot_duplicated", "参考图槽位重复", slot_key=key))
        seen.add(key)


def compile_storyboard_image(db: Session, shot_id: str, *,
                             workflow: Workflow | None = None) -> CompiledPrompt:
    """编译单个镜头的分镜图提示词：只组合镜头绑定且审核通过的角色/场景/道具。"""
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise LookupError(shot_id)
    scene = db.get(Scene, shot.scene_id)
    project = db.get(Project, scene.project_id) if scene is not None else None
    project_id = project.id if project is not None else ""

    blocking: list[dict] = []
    warnings: list[str] = []
    gate_issue = _review_gate(project)
    if gate_issue:
        blocking.append(gate_issue)

    caps = _capabilities(workflow)
    declared = _declared_slots(caps)
    declared_map = {item["param"]: item for item in declared}
    limit = _slot_limit(caps, declared)

    slots: list[dict] = []
    characters = _shot_characters(db, shot)
    prop_rows = _shot_props(db, shot, project_id)

    # 场景槽位
    if scene is not None:
        scene_asset, issue = _approved_asset(db, scene.baseline_asset_id, project_id, ("scene",))
        if issue:
            blocking.append({**issue, "entity_id": scene.id, "entity_type": "scene"})
        elif scene_asset is None:
            blocking.append(_issue("scene_baseline_missing",
                                    "场景缺少已审核的基准图，无法作为分镜参考图",
                                    entity_id=scene.id))
        elif "ref_scene" not in declared_map:
            # 工作流未声明场景槽位时降级为文字描述，与道具槽位策略一致：
            # 能力缺失不当作结构性错误硬阻断，仅显式告警保证不静默丢信息。
            warnings.append(
                "scene_reference_unsupported: 当前工作流无 ref_scene 槽位，场景仅以文字描述呈现"
            )
        else:
            _append_slot(slots, declared_map, "ref_scene", entity_id=scene.id, entity_type="scene",
                         asset=scene_asset, role="scene_layout_reference",
                         blocking=blocking, asset_label="场景")
    # 角色槽位：按工作流声明顺序取所有角色槽（ref_identity/ref_costume/ref_character_N…），
    # 槽位数量完全由工作流决定，不再写死两个名字
    from app.services.ref_inject import is_character_slot
    character_slot_pool = [item["param"] for item in declared if is_character_slot(item["param"])]
    if characters and not character_slot_pool:
        # 工作流未声明任何角色槽位（纯文生图）时降级为文字描述，与场景/道具槽位策略一致；
        # 真正的『槽位不够用』仍由下方 index 越界阻断，不静默丢图。
        warnings.append(
            "character_reference_unsupported: 当前工作流无角色参考槽位，角色仅以文字描述呈现"
        )
    for index, character in enumerate(characters):
        asset, issue = _approved_asset(db, character.portrait_asset_id, project_id, ("character",))
        if issue:
            blocking.append({**issue, "entity_id": character.id, "entity_type": "character"})
            continue
        if asset is None:
            blocking.append(_issue("character_portrait_missing",
                                    "角色缺少已审核的定妆图，无法作为分镜参考图",
                                    entity_id=character.id))
            continue
        if character_slot_pool and index >= len(character_slot_pool):
            # 仅『有角色槽位但数量不够用』才算能力超限；池为空属工作流不声明角色参考，
            # 已在上方降级为告警，此处不再阻断。
            blocking.append(_issue(
                "reference_capacity_exceeded",
                "镜头角色数量超过当前工作流参考图槽位能力，请减少角色或改用支持更多槽位的工作流",
                capacity=len(character_slot_pool),
            ))
            continue
        if not character_slot_pool:
            # 工作流不声明角色参考槽位：已在上方告警降级为纯文字描述，此处不注入槽位。
            continue
        _append_slot(slots, declared_map, character_slot_pool[index], entity_id=character.id,
                     entity_type="character", asset=asset, role="character_identity_reference",
                     blocking=blocking, asset_label="角色")


    # 道具槽位：工作流不支持时降级为文字描述并显式告警（不静默丢图）
    for prop in prop_rows:
        asset, issue = _approved_asset(db, prop.anchor_asset_id, project_id, ("prop",))
        if issue:
            blocking.append({**issue, "entity_id": prop.id, "entity_type": "prop"})
            continue
        if asset is None:
            warnings.append(f"prop_asset_missing: 道具 {prop.name} 无审核通过锚定图，仅以文字描述")
            continue
        if "ref_prop" not in declared_map:
            warnings.append(f"prop_reference_unsupported: 当前工作流无 ref_prop 槽位，道具 {prop.name} 仅文字描述")
            continue
        _append_slot(slots, declared_map, "ref_prop", entity_id=prop.id, entity_type="prop",
                     asset=asset, role="prop_identity_reference", blocking=blocking,
                     asset_label="道具")

    if limit and len(slots) > limit:
        blocking.append(_issue("reference_capacity_exceeded", "参考图数量超过工作流上限",
                                count=len(slots), limit=limit))
    _assert_unique_slots(slots, blocking)

    style = _style_for(db, project, "storyboard")
    style_snap = style_config.style_snapshot((project.settings if project else {}) or {},
                                             project_id=project.id if project else None)
    beats = shot.beats or []
    action = _text(beats[0] if beats else (shot.dialogue or ""), "镜头动作未提供")
    def _image_no(slot: dict, fallback: int) -> int:
        """槽位对应的提示词编号：取 input_key 的 image_N，保证与工作流实际连线一致。"""
        item = declared_map.get(slot.get("slot_key") or "") or {}
        m = re.search(r"image_(\d+)\s*$", str(item.get("input_key") or ""))
        return int(m.group(1)) if m else fallback

    ref_parts: list[str] = []
    scene_slot = next((s for s in slots if s["role"] == "scene_layout_reference"), None)
    if scene_slot is not None:
        ref_parts.append(
            f"以 <image{_image_no(scene_slot, 1)}> 为场景编辑底图，构图、透视与光线方向必须与其严格一致"
        )
    for character in characters:
        slot = next((s for s in slots
                     if s["entity_id"] == character.id and s["role"].startswith("character")), None)
        if slot is not None:
            ref_parts.append(
                f"<image{_image_no(slot, 2)}> 为角色「{character.name}」：脸部、体型、服装、配色完全取自该图，禁止与其他参考图混合"
            )
    for prop in prop_rows:
        slot = next((s for s in slots
                     if s["entity_id"] == prop.id and s["role"] == "prop_identity_reference"), None)
        if slot is not None:
            ref_parts.append(
                f"<image{_image_no(slot, 6)}> 为道具「{prop.name}」：形状、颜色、材质与辨识特征完全取自该图"
            )
    ref_bindings = "；".join(ref_parts) or "无参考图绑定，全部按文字描述生成"
    scene_target = "无场景参考底图，按文字描述重建场景"
    if scene_slot is not None:
        scene_target = (
            f"以 <image{_image_no(scene_slot, 1)}> 为场景编辑底图，构图、透视与光线方向必须与其严格一致"
        )
    mapping = {
        "shot_id": shot.id,
        "characters": "、".join(c.name for c in characters) or "无（空镜）",
        "scene": scene.title if scene is not None and scene.title else "未指定场景",
        "props": "、".join(p.name for p in prop_rows) or "无",
        "action": action,
        "positions": "按已审核的参考图与镜头动作安排角色相对位置",
        "prop_interaction": "道具只按镜头绑定关系出现在角色手中或环境里",
        "emotion": shot.emotion or "neutral",
        "shot_size": _text(shot.shot_type),
        "camera_angle": _text(shot.camera_angle),
        "composition": "主体清晰、空间关系合理",
        "camera_movement": _text(shot.camera_movement, "固定机位"),
        "lighting": "光从左上方照下，投影落向右下，与场景板主光方向一致",
        "character_slots": ", ".join(s["slot_key"] for s in slots
                                     if s["role"].startswith("character")) or "无",
        "scene_slot": next((s["slot_key"] for s in slots if s["role"] == "scene_layout_reference"), "无"),
        "prop_slots": ", ".join(s["slot_key"] for s in slots
                                 if s["role"] == "prop_identity_reference") or "无",
        "scene_target": scene_target,
        "ref_bindings": ref_bindings,
        "style": style,
    }

    meta = TEMPLATE_REGISTRY[TEMPLATE_STORYBOARD]
    return CompiledPrompt(
        task_type=TASK_STORYBOARD_IMAGE,
        template_id=TEMPLATE_STORYBOARD,
        template_version=int(meta["version"]),
        entity_id=shot.id,
        asset_type="keyframe",
        positive_prompt=render_template(TEMPLATE_STORYBOARD, mapping),
        negative_prompt=negative_for("keyframe"),
        reference_slots=slots,
        generation_constraints={
            "shot_id": shot.id,
            "character_ids": [c.id for c in characters],
            "scene_id": scene.id if scene is not None else None,
            "prop_ids": [p.id for p in prop_rows],
            "max_reference_images": limit,
            "aspect_ratio": project.aspect_ratio if project is not None else None,
            "workflow_id": workflow.id if workflow is not None else None,
            "style_config": style_snap,
        },
        blocking_issues=blocking,
        warnings=warnings,
        canonical_snapshot={"shot_id": shot.id, "scene_id": shot.scene_id,
                            "character_ids": [c.id for c in characters],
                            "style": style_snap,
                            "template_id": TEMPLATE_STORYBOARD,
                            "template_version": int(meta["version"])},
    )


# ── 分镜视频编译 ──────────────────────────────────────────────
def _previous_shot(db: Session, shot: Shot) -> Shot | None:
    """只取同场景同集的上一镜。

    跨场景/跨集的末帧衔接不成立（API.md §8）：硬把上一场景最后一镜的尾帧当作
    当前镜头首帧，会产出场景硬切的怪画面，因此这里不跨场景回退。
    """
    return (db.query(Shot)
            .filter(Shot.scene_id == shot.scene_id, Shot.episode == shot.episode,
                    Shot.seq < shot.seq)
            .order_by(Shot.seq.desc()).first())


def _has_earlier_shot_anywhere(db: Session, shot: Shot) -> bool:
    """是否存在更早的镜头（含跨场景/跨集）。

    用于区分两种『找不到同场景上一镜』：
    - 存在更早镜头 → 属跨场景情形，按文档降级为文字衔接，不阻断；
    - 完全不存在更早镜头 → requires_prev_frame 本身是数据错误，必须阻断。
    """
    return (db.query(Shot)
            .filter(Shot.episode == shot.episode, Shot.seq < shot.seq)
            .first()) is not None


def compile_shot_video(db: Session, shot_id: str, *,
                       workflow: Workflow | None = None) -> CompiledPrompt:
    """编译分镜视频提示词：延续已审核分镜图，连续性来源显式绑定到镜头与文件。"""
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise LookupError(shot_id)
    scene = db.get(Scene, shot.scene_id)
    project = db.get(Project, scene.project_id) if scene is not None else None
    project_id = project.id if project is not None else ""

    blocking: list[dict] = []
    warnings: list[str] = []
    caps = _capabilities(workflow)
    declared_map = {item["param"]: item for item in _declared_slots(caps)}

    if not shot.keyframe_confirmed:
        blocking.append(_issue("storyboard_not_approved", "分镜图尚未确认，禁止生成分镜视频"))

    # Asset 无 shot_id 字段（见 DATABASE.md），镜头产物经 source_job_id → job.shot_id 关联
    storyboard = (db.query(Asset)
                  .join(Job, Asset.source_job_id == Job.id)
                  .filter(Job.shot_id == shot.id, Asset.asset_type == "keyframe",
                          Asset.validity == "current")
                  .order_by(Asset.version.desc()).first())
    if storyboard is None:
        blocking.append(_issue("storyboard_asset_missing", "缺少当前有效的分镜图资产"))
    elif not _media_file_exists(storyboard.file_path):
        blocking.append(_issue("storyboard_file_missing", "分镜图文件不存在",
                                asset_id=storyboard.id))

    slots: list[dict] = []
    prev = _previous_shot(db, shot)
    source_shot_id = None
    source_file_id = None
    continuity_type = "none"
    if shot.requires_prev_frame:
        continuity_type = "previous_shot_last_frame"
        if prev is None:
            if _has_earlier_shot_anywhere(db, shot):
                # 跨场景/跨集：不注入末帧，改由 start_state 文字衔接（API.md §8）。
                continuity_type = "cross_scene_text_bridge"
                warnings.append(
                    "prev_frame_skipped: 上一镜不在同场景同集，已跳过末帧注入，"
                    "改由 start_state 文字衔接"
                )
            else:
                blocking.append(_issue("prev_frame_missing", "声明需要上一镜尾帧，但找不到上一镜"))
        else:
            source_shot_id = prev.id
            asset, issue = _approved_asset(db, prev.last_frame_asset_id, project_id, ("last_frame",))
            if issue:
                blocking.append({**issue, "entity_id": prev.id, "entity_type": "last_frame"})
            elif asset is None:
                blocking.append(_issue("prev_frame_missing", "上一镜缺少可用的尾帧文件",
                                        source_shot_id=prev.id))
            elif "ref_prev_frame" not in declared_map:
                blocking.append(_issue("reference_capacity_exceeded",
                                        "当前视频工作流不支持 ref_prev_frame 槽位",
                                        slot_key="ref_prev_frame"))
            else:
                source_file_id = asset.id
                _append_slot(slots, declared_map, "ref_prev_frame", entity_id=prev.id,
                             entity_type="last_frame", asset=asset, role="previous_shot_last_frame",
                             blocking=blocking, asset_label="上一镜尾帧")

    fps = int(caps.get("fps") or 24)
    duration = float(shot.duration or 10.0)
    step = int(caps.get("frame_count_step") or 17)
    offset = int(caps.get("frame_count_offset") or 5)
    frame_range = caps.get("frame_count_range") or [124, 362]
    raw_frames = round(duration * fps)
    frames = offset + step * max(1, round((raw_frames - offset) / step))
    min_frames, max_frames = frame_range[0], frame_range[1]
    if frames < min_frames:
        # 低于工作流下限时不再静默拉长：把 1 秒的甩镜拉成 5 秒会让动作语义完全失真，
        # 由调用方决定拆分镜头或延长时长，而不是悄悄给出一个『看起来正常』的长视频。
        blocking.append(_issue(
            "duration_below_workflow_min",
            f"镜头时长 {duration:g}s 折算 {frames} 帧，低于工作流下限 {min_frames} 帧"
            f"（约 {min_frames / fps:.2f}s），请拆分镜头或延长时长",
            duration=duration, frames=frames, min_frames=min_frames,
        ))
    frames = max(min_frames, min(max_frames, frames))
    if frames > max_frames:
        warnings.append(
            f"duration_clamped_to_max: 折算 {frames} 帧超过工作流上限 {max_frames} 帧，已截断"
        )
    if abs(frames / fps - duration) > 1.0:
        warnings.append(
            f"duration_adjusted: 目标时长 {duration}s 按工作流帧率约束折算为 {frames} 帧"
            f"（{frames / fps:.2f}s）"
        )

    characters = _shot_characters(db, shot)
    prop_rows = _shot_props(db, shot, project_id)
    style = _style_for(db, project, "shot_video")
    style_snap = style_config.style_snapshot((project.settings if project else {}) or {},
                                             project_id=project.id if project else None)
    mapping = {
        "shot_id": shot.id,
        "duration": f"{duration:g}",
        "characters": "、".join(c.name for c in characters) or "无",
        "scene": scene.title if scene is not None and scene.title else "未指定场景",
        "props": "、".join(p.name for p in prop_rows) or "无",
        "start_state": _text(shot.start_state, "延续已审核分镜图初始状态"),
        "main_action": _text(shot.beats, "按已审核分镜图完成镜头动作"),
        "end_state": _text(shot.end_state, "延续动作的自然收束状态"),
        "shot_size": _text(shot.shot_type),
        "camera_angle": _text(shot.camera_angle),
        "camera_movement": _text(shot.camera_movement, "固定机位"),
        "pacing": _text(None, "平稳节奏"),
        "environment_changes": "无场景切换",
        "continuity_in_type": continuity_type,
        "source_shot_id": source_shot_id or "无",
        "source_frame_file_id": source_file_id or "无",
        "continuity_out_state": _text(None, "按镜头动作自然结束"),
        "dialogue": ("no dialogue" if not (shot.dialogue or "").strip()
                     else f"(S1) <d>{shot.dialogue.strip().replace(chr(10), ' ')}</d> while the character speaks on screen"),
        "soundscape": _text(shot.soundscape, "N/A"),
        "music": "N/A",
        "style": style,
    }

    meta = TEMPLATE_REGISTRY[TEMPLATE_VIDEO]
    return CompiledPrompt(
        task_type=TASK_SHOT_VIDEO,
        template_id=TEMPLATE_VIDEO,
        template_version=int(meta["version"]),
        entity_id=shot.id,
        asset_type="clip",
        positive_prompt=render_template(TEMPLATE_VIDEO, mapping),
        negative_prompt=negative_for("clip"),
        reference_slots=slots,
        generation_constraints={
            "shot_id": shot.id,
            "duration_seconds": duration,
            "fps": fps,
            "frame_count": frames,
            "continuity_in": {"type": continuity_type, "source_shot_id": source_shot_id,
                              "source_file_id": source_file_id},
            "storyboard_asset_file_id": storyboard.id if storyboard is not None else None,
            "workflow_id": workflow.id if workflow is not None else None,
            "style_config": style_snap,
        },
        blocking_issues=blocking,
        warnings=warnings,
        canonical_snapshot={"shot_id": shot.id, "scene_id": shot.scene_id,
                            "character_ids": [c.id for c in characters],
                            "style": style_snap,
                            "template_id": TEMPLATE_VIDEO,
                            "template_version": int(meta["version"])},
    )


# ── 校验与落地 ───────────────────────────────────────────────
def validate_compilation(payload: dict) -> CompiledPrompt:
    """对编译结果做 JSON Schema 校验（严格模式），不合法直接拒绝提交生成任务。"""
    from app.schemas.prompt_compilation import CompiledPromptModel

    model = CompiledPromptModel.model_validate(payload)
    data = model.model_dump()
    compiled = CompiledPrompt(
        task_type=data["task_type"],
        template_id=data["template_id"],
        template_version=int(data["template_version"]),
        entity_id=data["entity_id"],
        asset_type=data.get("asset_type"),
        positive_prompt=data["positive_prompt"],
        negative_prompt=data["negative_prompt"],
        reference_slots=data.get("reference_slots") or [],
        generation_constraints=data.get("generation_constraints") or {},
        blocking_issues=data.get("blocking_issues") or [],
        warnings=data.get("warnings") or [],
        canonical_snapshot=data.get("canonical_snapshot") or {},
    )
    keys = [slot["slot_key"] for slot in compiled.reference_slots]
    if len(keys) != len(set(keys)):
        compiled.blocking_issues.append(_issue("reference_slot_duplicated", "参考图槽位重复"))
    # Schema 层不校验文件系统，此处补上：注入了却不存在的参考图会污染工作流输出，
    # 且 prune_unbound_media_inputs 只能剔除『未注入』槽位，救不了『注入了但文件缺失』。
    for slot in compiled.reference_slots:
        if not _media_file_exists(slot.get("file_path")):
            compiled.blocking_issues.append(_issue(
                "reference_asset_file_missing",
                f"参考图文件不存在或不可读：{slot['slot_key']}",
                slot_key=slot["slot_key"], file_path=slot.get("file_path"),
            ))
    return compiled


def apply_to_params(compiled: CompiledPrompt, params: dict) -> dict:
    """把编译结果写入生成参数：提示词、负向词、参考图槽位（槽位名保持工作流声明）。"""
    if compiled.status != "ready":
        raise ValueError("prompt_compilation_not_ready")
    out = dict(params)
    # 调用方已显式给定提示词时不覆盖：编译结果只补充缺失项，避免静默改写用户输入。
    if not str(out.get("prompt") or "").strip():
        out["prompt"] = compiled.positive_prompt
    if compiled.negative_prompt and not str(out.get("negative") or "").strip():
        out["negative"] = compiled.negative_prompt
    if compiled.reference_slots:
        media = dict(out.get("__media__") or {})
        for slot in compiled.reference_slots:
            media[slot["slot_key"]] = slot["file_path"]
        out["__media__"] = media
    return out


def assert_prompt_only_patch(original: dict, patch: dict) -> dict:
    """『只修改提示词』模式：锁定模型/工作流/尺寸/时长与全部参考图，只允许改提示词。"""
    locked_keys = {"__media__", "width", "height", "length", "seed", "workflow_id"}
    illegal = [key for key in patch if key in locked_keys]
    if illegal:
        raise ValueError(f"prompt_only_patch_locked_fields: {','.join(sorted(illegal))}")
    merged = dict(original)
    if "prompt" in patch:
        merged["prompt"] = patch["prompt"]
    if "negative" in patch:
        merged["negative"] = patch["negative"]
    return merged
