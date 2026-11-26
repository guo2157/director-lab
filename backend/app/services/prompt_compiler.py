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
import os
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_
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
    TEMPLATE_CHARACTER: {"version": 2, "task_type": TASK_ASSET_IMAGE, "asset_type": "character",
                         "prompt_name": "compile_character_asset"},
    TEMPLATE_SCENE: {"version": 2, "task_type": TASK_ASSET_IMAGE, "asset_type": "scene",
                     "prompt_name": "compile_scene_asset"},
    TEMPLATE_PROP: {"version": 2, "task_type": TASK_ASSET_IMAGE, "asset_type": "prop",
                    "prompt_name": "compile_prop_asset"},
    TEMPLATE_STORYBOARD: {"version": 3, "task_type": TASK_STORYBOARD_IMAGE, "asset_type": "keyframe",
                          "prompt_name": "compile_storyboard_image"},
    TEMPLATE_VIDEO: {"version": 4, "task_type": TASK_SHOT_VIDEO, "asset_type": "clip",
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
    "多角色拼图，不同角色同框，身份漂移，面孔不一致，服装配饰不一致，年龄感不一致，"
    "配饰缺失或新增，"
    "第二个人物，重复角色，人物剪影，镜中人物，背景人物，额外肢体，裁切主体，无关物品，"
    "多视图，风格混杂，严重变形"
)
SCENE_NEGATIVE = (
    "角色，人物，群众，动物，人物剪影，人物倒影，角色海报，手，身体局部，剧情表演，"
    "未定义的关键道具，额外场景，多个地点拼接，分屏，空间结构混乱，地面与墙面融合，"
    "建筑结构错误，重复家具，杂乱堆放，过多装饰，未经定义的文字标牌，空间结构漂移，"
    "透视漂移，光线方向矛盾，阴影方向不一致，地平线倾斜，"
    "人体局部，无关道具，多场景组合，重复建筑，空间结构错误，背景杂乱，严重模糊"
)
PROP_NEGATIVE = (
    "多个相同道具，多件道具合集，道具堆叠，其他道具，人物，角色，手，手臂，身体，"
    "角色正在使用道具，场景背景，桌面杂物，包装盒，标签，说明文字，尺寸标尺，"
    "未定义配件，形状改变，颜色错误，材质错误，比例错误，结构缺失，主体被裁切，遮挡，"
    "重复部件，悬浮部件，结构融合，材质漂移，外形漂移，表面纹理不一致，"
    "部件数量增减，使用痕迹臆造，"
    "多个重复物品，无关物品，手部，结构变形，主体裁切"
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

# ── 历史 P1 负向词层已整层并入类型层（P3-2 完成，2026-11-26）──────────
# 原三层（character / scene / prop）含 23 个类型层与通用层都没有的独有词
# （character 11 / scene 7 / prop 5），已按原有相对顺序追加到对应类型层末尾，
# 历史层随之删除。
#
# merge_negative 按「通用层 → 类型层」的首次出现顺序去重，因此把独有词
# 追加到类型层末尾后，去重结果与原先「通用层 → 类型层 → 历史层」完全等价：
# 五种类型 negative_for 输出逐字符一致（实测 character 422 / scene 337 /
# prop 337 / clip 311 / keyframe 364 字符，全部 identical=True）。
#
# 早期判断「并入会改词序」经复核为误判：当时的并入方式把独有词插到了类型层
# 中间，本次改为严格保持相对顺序的末尾追加，故不再需要 A/B 出图抽检。
# 后续调整负向词请直接改上面的类型层常量，不要再引入历史层。

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
视图规范（单视图为默认交付，直接作为分镜身份参考）：
- 默认只输出一张正面全身定妆照：画面中只有这一个角色、只有一个视角、正面朝向镜头
- 全身完整入画，头顶到脚底全部可见，主体居中，身形比例真实自然
- 不做并排多视角拼图：三视图会让模型同时接收到三张人脸，作为身份参考注入分镜时会诱发角色分身与多人同框
- 如确需三视图（正面/侧面/背面）作角色设定交付，必须在提示词中显式单独要求；该交付物不得作为分镜身份参考图使用

纯净度要求：画面中只有这一个角色，不出现第二个人物、人物剪影、镜中人物、背景人物、路人、动物；不出现文字、标签、编号、尺寸标线、水印、边框、拼贴或分格线。

画面限制：默认输出单张正面全身定妆照；不生成表情合集、多角色拼图、不同角色同框、并排多视角拼图；不生成半身像或只有头部的特写替代全身视图。
画面风格：{style}"""

SCENE_TEMPLATE = """场景板（空镜）：{name}，{time_and_weather}，{empty_expr}，只有景色与建筑。
前景：{foreground}，地面材质与近处结构真实清晰，地平面必须水平。
中景：{midground}。中景必须保留一段可见的干净地面与纵深作为后续人物合成的站位区，接触阴影有处可落，人物有立足点。
远景：{background}，层次分明，有雾气或纵深过渡。
光线：{lighting}，光源方向单一且写死，地面反光、玻璃反光与阴影长度全部按此方向一致；画面只允许一个投影方向。
镜头：{camera_shot}，前中后景层次清晰。
质感：{texture}，主色调与材质：{palette}。
环境定义：{identity}
可站位物件清单（用于下游角色站位对账，硬性）：必须在生成场景基准图的同时，识别并输出本场景中所有角色可站立于旁、可就座、可倚靠的固定物件，每件标注名称、图上大致框区、按透视推算的俯视平面位置、是否可就座、是否为圆形台面。此清单将作为本场所有分镜提示词中 {standable_items} 的来源，后续站位只允许落在清单内的物件上。
- 站位物件：{standable_items}
允许出现的固定环境元素：{fixed_elements}。
不得出现的剧情道具、车辆与可移动杂物：{excluded_props}。
纯净度要求（硬性，逐条核对）：
- 没有任何人：不出现人物、行人、群众、角色剪影、人体局部、背影，也不出现远景中的小黑人形
- 没有间接人影：不出现角色倒影、水面人形倒影、墙上海报人物、雕像、人形立牌、照片中的人
- 没有动物与载具：不出现动物、车辆、飞行器
- 没有剧情道具：不出现剧本中可移动的物品，只保留属于环境本身的固定构造
- 没有文字与装饰：不出现文字、招牌、路牌、海报、价目表、门牌号、Logo、水印、边框、分格线（需要门头时写纯色无字招牌）
整个画面只由环境本身与天空构成。
画面风格：{style}"""

# 道具锚定图只输出单一视角：并排多视图（正视+侧视）会让下游分镜图复制这种分格排版，
# 把单帧画面生成成多格拼图（实测 ast_25f8313d91c3 两视图导致第 3、6 镜出现四格拼图）。
PROP_TEMPLATE = """生成一张独立的道具参考图，画面中只能有一件道具：{name}。
道具定义：{identity}
形状与结构：{shape}
颜色：{color}
材质与表面细节：{material}
相对尺寸与比例：{relative_scale}
必须保留的关键细节：{distinctive_features}
构图要求：整件道具完整入画、主体居中、轮廓清晰、形状容易辨认，采用干净统一的纯色或低干扰背景；以独立物品展示，不放进故事场景，不表现使用过程。
视图规范（单视图为默认交付，直接作为分镜道具参考）：
- 单件道具、单一视角：画面中只有这一件道具，只从一个角度（正面或轻微四分之三视角）完整展示
- 不做并排多视角拼图，不做正视与侧视的组合排版，画面中不出现分格线
- 材质必须可辨识：金属有反光与高光、木质有纹理、布料有织物结构、玻璃有透光与折射

一致性要求：不增加数量、装饰、图案、附件或包装；同一件道具的多部件结构整体展示，不拆成多件独立物品；不凭空添加磨损、划痕、锈迹等剧本未描述的痕迹。

纯净度要求（硬性，逐条核对）：
- 没有任何人：不出现角色、手部、手指、手臂、人体局部、人物剪影
- 没有使用场景：不放进故事发生的环境，不表现被使用、被握持、被放置的瞬间
- 没有其他物品：不出现第二个道具、道具堆叠、桌面杂物、背景摆件、包装盒
- 没有文字与装饰：不出现标签、说明文字、尺寸标线、价格牌、Logo、水印、边框、分格线
背景为干净统一的纯色或低干扰背景，画面只由这一件道具构成。

画面风格：{style}"""

# 参考图职责行模板：compile_storyboard_image 生成 ref_duty_block，
# prompt_qc._ROLE_KEEP 需保持同口径（否则 relabel_reference_block 会改回去）
_DUTY_LINE = {
    "scene":     "- <image{n}>（场景基准图）：@{name} —— 仅用作背景的空间结构、地形、植被、光线方向与色调；不要把它缩小、平铺或原样放进画面。",
    "character": "- <image{n}>（角色定妆图）：@{name} —— 仅用作该角色的面部、发型、体型与服装；禁止与其他参考图混合。",
    "prop":      "- <image{n}>（道具锚定图）：@{name} —— 仅用作该道具的形状、颜色与材质；画面中只出现这一个道具、一个视角。",
}

def _aspect_desc(ratio: str | None, width: int | None = None, height: int | None = None) -> str:
    """把项目 aspect_ratio 转成中文画幅描述，如 '竖构图 9:16'。"""
    if ratio:
        parts = ratio.split(":")
        if len(parts) == 2:
            try:
                w, h = int(parts[0]), int(parts[1])
                orient = "竖构图" if h > w else ("横构图" if w > h else "方构图")
                return f"{orient} {ratio}"
            except (ValueError, TypeError):
                pass
        return ratio
    if width and height:
        orient = "竖构图" if height > width else ("横构图" if width > height else "方构图")
        return f"{orient} {width}×{height}"
    return "默认比例"

STORYBOARD_TEMPLATE = """任务：生成镜头 {shot_id} 的一张完整单帧电影画面。一个镜头、一个瞬间、一个连续空间，画面铺满整个画布（{aspect_desc}）。
参考图职责：
{ref_duty_block}
目标画面：在场景「{scene}」中，{action_moment}。
{plan_posture_block}
角色站位（不可互换）：
{position_specs}
站位规则：
- 本场首镜：站位、朝向、坐站状态以 {opening_blocking} 为唯一真值，后续从该锚点接续；非首镜与上一镜末态视觉连贯，不凭空换边、换座、换朝向
- 各角色只站在基准图实有物件旁，可站立物件：{standable_items}
- 同一物件同侧不叠人；多人相邻留清晰间隙
- 视线落点：{gaze_targets}——说话时看对手，沉默时眼神方向符合情绪与环境
道具与角色或环境的关系：{prop_interaction}。
画面情绪：{emotion}——用表情、眼神与姿态外化，不用抽象情绪词。
镜头语言：景别 {shot_size}；角度 {camera_angle}；构图 {composition}；运动 {camera_movement}；光线与色彩 {lighting}。若各参考图光线不同，以场景基准图的光线为准。
必须保持：
- 人物数量精确：{character_count_rule}
- 不出现未绑定人物：旁白人物、路人、群众、背景人群、剪影、人体局部、照片里的人
- 不出现未绑定道具：只出现 {props}，不加其他可移动物品、车辆、杂物、包装
- 场景元素只用底图：背景、建筑结构、地形、植被取自场景参考图，不新增未定义的建筑、招牌、家具、装饰，不改动空间结构
- 画面连续：地面接触阴影真实，人物有立足点，不悬浮、不穿模、不与道具或环境融合
输出：一张{aspect_desc}的完整画面，单一视角、单一瞬间，所有内容自然连成一个整体；不含文字、字幕、标签、水印、边框与分格线。
画面风格：{style}
"""

VIDEO_TEMPLATE = """integrated_multimodal_description:
Scene: {scene}. {character_count} characters present, props: {props}. Overall style: {style}. {continuity_lock}
Reference bindings: {ref_bindings}. Do not alter face, body type, costume, color palette or proportions. {spatial_lock}

[Shot 1] Character positions (one anchor per character, do not swap):
{position_specs}
{screen_geography} {main_action}. Start state: {start_state}. End state: {end_state}. Shot size: {shot_size}. Camera: {camera_angle}. Movement: {camera_movement}. Pacing: {pacing}. Environment changes: {environment_changes}. Continuity in: {continuity_in_type}; source shot: {source_shot_id}; source frame: {source_frame_file_id}. Continuity out: {continuity_out_state}. Dialogue: {dialogue}.

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
    # 模板内容指纹（P2-1）：实际生效模板（外置优先）的 sha256 前 12 位，写入任务快照供追溯
    content_hash: str = ""

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
            "content_hash": self.content_hash,
            "positive_prompt": self.positive_prompt,
            "negative_prompt": self.negative_prompt,
            "reference_slots": self.reference_slots,
            "generation_constraints": self.generation_constraints,
            "blocking_issues": self.blocking_issues,
            "warnings": self.warnings,
            "canonical_snapshot": self.canonical_snapshot,
        }


def _template_hash(meta: dict) -> str:
    """当前生效模板内容的 hash（P2-1）：外置优先、内置回退，失败时返回空串不阻断编译。"""
    from app.services.prompt_registry import template_content_hash

    try:
        return template_content_hash(str(meta.get("prompt_name") or ""))
    except Exception:  # noqa: BLE001 - 指纹缺失不得阻断编译主链路
        return ""


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


# start_state / end_state 六项枚举的中文标签，用于把字典渲染成带键名的可读句子。
# 直接走 _text() 会把 values 用「、」拼接，键名与字段含义全部丢失
# （如「马克笔」看不出是手持物、「无」来自 injury 字段），且多角色镜头共用一句。
_STATE_LABELS: dict[str, str] = {
    "pose": "姿态",
    "position": "位置",
    "expression": "表情",
    "holding": "手持",
    "costume": "服装",
    "injury": "伤势",
}


def _describe_state(value: Any, fallback: str = "未提供") -> str:
    """把 start_state / end_state 渲染为「姿态：站立；位置：镜头前；…」。

    跳过空值与「无」，避免拼出无意义的尾巴；未知键保留原键名，不静默丢弃。
    """
    if value is None:
        return fallback
    if not isinstance(value, dict):
        return _text(value, fallback)
    parts: list[str] = []
    for key, raw in value.items():
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text in {"无", "None", "null", "-"}:
            continue
        label = _STATE_LABELS.get(str(key), str(key))
        parts.append(f"{label}：{text}")
    return "；".join(parts) if parts else fallback


def _describe_beats(value: Any, fallback: str = "按已审核分镜图完成镜头动作") -> str:
    """把 beats 列表渲染为「第1拍 动作（起始 → 结束，约2秒）；…」。

    此前 _text() 对 list[dict] 走 str(v)，会把 Python 字典原文
    （单引号、花括号、start_pose 等英文字段名）直接写进英文提示词正文。
    """
    if not value:
        return fallback
    if not isinstance(value, (list, tuple)):
        return _text(value, fallback)
    parts: list[str] = []
    for index, beat in enumerate(value, start=1):
        if not isinstance(beat, dict):
            text = str(beat).strip()
            if text:
                parts.append(text)
            continue
        action = str(beat.get("action") or "").strip()
        start_pose = str(beat.get("start_pose") or "").strip()
        end_pose = str(beat.get("end_pose") or "").strip()
        duration = beat.get("duration")
        seg = f"第{index}拍 {action or '动作延续'}"
        if start_pose or end_pose:
            seg += f"（{start_pose or '自然起始'} → {end_pose or '自然收束'}"
            if duration:
                seg += f"，约{duration}秒"
            seg += "）"
        elif duration:
            seg += f"（约{duration}秒）"
        parts.append(seg)
    return "；".join(parts) if parts else fallback


def merge_negative(*parts: str) -> str:
    """合并负向提示词：按中英文分隔符切分后去重，保持首次出现顺序。"""
    seen: list[str] = []
    for part in parts:
        for term in re.split(r"[，,、;；]\s*", part or ""):
            item = term.strip()
            if item and item not in seen:
                seen.append(item)
    return "，".join(seen)


def effective_cfg(definition: dict | None) -> float | None:
    """读取工作流中 CFGGuider 的 cfg 实际取值。

    classifier-free guidance 的采样公式为 neg + cfg * (pos - neg)：
    当 cfg=1 时该式退化为 pos，负向分支被数学抵消，写入 negative 参数不会影响出图。
    返回 None 表示工作流没有 CFGGuider（例如 BasicGuider），无法判定。
    """
    if not isinstance(definition, dict):
        return None
    for node in definition.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "CFGGuider":
            continue
        value = (node.get("inputs") or {}).get("cfg")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def negative_for(asset_or_task_type: str) -> str:
    """返回『通用基础层 + 类型专用层』合并后的负向提示词。"""
    specific = _NEGATIVE_BY_TYPE.get(asset_or_task_type, "")
    return merge_negative(COMMON_NEGATIVE, specific)


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
    """归一化工作流参考槽位声明，统一走 capabilities.declared_reference_slots。

    数据库中 workflow.capabilities 实际保存的是 normalize() 产出的
    reference.slots，直接读顶层 reference_slots 只会得到 None，
    会把「已声明槽位」误判成「未声明」，场景/角色参考被整体降级为文字描述。
    """
    from app.services.workflow.capabilities import declared_reference_slots
    slots: list[dict] = []
    for item in declared_reference_slots(capabilities):
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
    if not (isinstance(limit, int) and limit > 0):
        # 归一化能力把参考图上限放在 reference.max_image_references
        ref = capabilities.get("reference")
        if isinstance(ref, dict):
            limit = ref.get("max_image_references")
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
    # 底图卫兵：质检未通过的资产不得被下游引用。
    # 场景基准图一旦混入人物或被裁切，会污染所有引用它的分镜图与镜头，
    # 在引用边界拦住的成本远低于事后逐镜重生成。
    # 只拦 failed：pending 表示尚未质检（历史资产或 mock 模式），不能当作不合格，
    # 否则存量项目会整体无法出图。
    if (getattr(asset, "quality_status", None) or "") == "failed":
        return None, _issue(
            "reference_asset_quality_failed",
            "参考图质量检查未通过，不能作为生成参考",
            asset_id=asset_id,
            quality_issues=asset.quality_issues,
        )
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
            "standable_items": "场景板生成后由 LLM 提取并填写，列出本场景中所有角色可站立于旁、可就座、可倚靠的固定物件（名称/图上框区/俯视平面位置/是否可坐/是否圆形）",
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
        "standable_items": "场景板生成后由 LLM 提取并填写，列出本场景中所有角色可站立于旁、可就座、可倚靠的固定物件（名称/图上框区/俯视平面位置/是否可坐/是否圆形）",
        "style": style or asset_prompts._DEFAULT_STYLE,
    }


def _prop_mapping(prop: Prop, style: str | None = None) -> dict:
    data = _as_dict(prop.appearance)
    # appearance 为纯文本时是道具的视觉外观定义；description 往往混入剧情用法
    # （如「此后一直由林晓捧在手中」），不能当作外观细节拼进提示词。
    appearance_text = _text(prop.appearance if isinstance(prop.appearance, str) else None, "")
    return {
        "name": prop.name or "未命名道具",
        "identity": appearance_text or _text(prop.description, "按道具定义"),
        "shape": _text(data.get("shape")),
        "color": _text(data.get("color")),
        "material": _text(data.get("material")),
        "relative_scale": _text(data.get("relative_scale")),
        # 「必须保留的关键细节」只取外观：实测把 description 里的「由林晓捧在手中」
        # 拼进提示词，模型会在道具锚定图里画出人手，而道具图要求空场无人物，
        # 质检直接判不合格并阻断所有引用该道具的分镜图（prp_b21f3590710f）。
        "distinctive_features": _text(
            data.get("distinctive_features"), appearance_text or "按道具定义"
        ),
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
        else:
            _cfg = effective_cfg(getattr(workflow, "definition", None))
            if _cfg is not None and _cfg <= 1.05:
                warnings.append(
                    f"negative_prompt_ineffective: 当前工作流 CFGGuider.cfg={_cfg}，"
                    "负向分支在 cfg=1 时被数学抵消，负向提示词不会生效；"
                    "约束请改写为正向肯定句，或改用非蒸馏（cfg 4-7）工作流"
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
        content_hash=_template_hash(meta),
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
    """镜头出场角色 —— 委托给 ShotCast 单一出场清单（Phase 2）。

    历史上这里和注入器、门禁各写了一份，三份定义逐渐分叉：本函数只认
    「主角 + 对白说话人」，注入器额外认「名字出现在 dialogue 正文」，
    门禁又只认前者，于是注入器放进去的配角被门禁判成换脸泄漏、整单 409。
    Phase 2 起统一读 ShotCast：有 shot_plan 契约时以契约为准（画外声音
    不占身份槽位、不计入人数约束），无契约走旧启发式兜底
    （shot_subject_characters：character_id + 对白说话人 + @引用 + 动作文本）。

    多参考图工作流（4 个角色槽）依赖这里拿到全部出场角色，
    否则对白里出现第二个角色时，媒体注入了它、提示词却没绑定它。
    """
    from app.services.shot_cast import resolve_shot_cast
    return [cc.character for cc in resolve_shot_cast(db, shot).characters]


def _shot_props(db: Session, shot: Shot, project_id: str) -> list[Prop]:
    """本镜涉及道具 —— 委托给 ShotCast 单一出场清单（Phase 2，P0-4 修复）。

    有 shot_plan 契约时以契约 visible_props 为准（经生命周期保护性合并）；
    无契约走旧口径（shot.props ∪ 提示词 @引用 ∪ PropShotState 记录）。
    只返回 reference_required 的道具：offscreen/occluded 不进「只出现」清单，
    与注入器（reference_resolver）的实际注入集合保持同一份出场清单——
    改造前提示词说「只出现 A、B」而参考图只注入 A 的矛盾（P0-4）由此消除。
    """
    from app.services.shot_cast import resolve_shot_cast
    return [cp.prop for cp in resolve_shot_cast(db, shot).props if cp.reference_required]


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


def _norm_path(value: str | None) -> str:
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(str(value)))


def _is_continuity_source_frame(db: Session, shot: Shot, file_path: str) -> bool:
    """跨集首帧来自合成视频解码帧，不登记为 Asset；与连续性记录逐路径比对。"""
    from app.models import EpisodeContinuity

    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return False
    target = _norm_path(file_path)
    rows = (
        db.query(EpisodeContinuity)
        .filter(
            EpisodeContinuity.project_id == scene.project_id,
            EpisodeContinuity.episode == shot.episode,
        )
        .all()
    )
    return any(row.source_frame_path and _norm_path(row.source_frame_path) == target for row in rows)


def audit_reference_slots(db: Session, shot: Shot, media: dict | None) -> list[dict]:
    """生成前校验参考图槽位职责，防止身份图与尾帧图串位（硬门禁）。

    规则（对应 Prompt 参考图职责整改）：
    - 角色身份槽（ref_identity/ref_character_N…）只能放本镜主体角色的定妆图；
      非主体角色身份图进入身份槽 → identity_reference_leak，必须阻断；
    - 时序槽（ref_prev_frame / first_frame）只能放尾帧：上一镜实际尾帧资产，
      或跨集连续性记录里的末帧解码帧；放分镜图/定妆图同样阻断；
    - ref_image 只能放当前镜头的分镜图，不允许用其他镜头或其他类型资产顶替。

    返回 blocking_issues 结构；调用方应按硬错误处理，不静默降级。
    """
    issues: list[dict] = []
    if not media or not isinstance(media, dict):
        return issues
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return issues
    project_id = scene.project_id

    wanted = {_norm_path(path): path for path in media.values() if path}
    assets: dict[str, Asset] = {}
    if wanted:
        for asset in db.query(Asset).filter(Asset.project_id == project_id).all():
            key = _norm_path(asset.file_path)
            if key and key in wanted:
                assets[key] = asset
    chars = db.query(Character).filter(Character.project_id == project_id).all()
    portrait_owner = {c.portrait_asset_id: c for c in chars if c.portrait_asset_id}
    # 主体角色集合必须与注入器同一口径，否则注入器放入的身份槽会被本门禁判泄漏
    # （实测：配角只在提示词里被 @引用、或只在 dialogue_segments 说话，两边定义
    # 不一致就会整单 409）。Phase 2 起统一读 ShotCast 单一出场清单：
    # 有 shot_plan 契约时画外声音不占身份槽位，无契约走旧启发式口径。
    from app.services.shot_cast import resolve_shot_cast
    subject_ids = set(resolve_shot_cast(db, shot).character_ids)
    from app.services.ref_inject import is_character_slot

    for slot, path in media.items():
        if not path:
            continue
        asset = assets.get(_norm_path(path))
        if is_character_slot(slot):
            owner = portrait_owner.get(asset.id) if asset is not None else None
            if owner is None:
                issues.append(_issue(
                    "reference_asset_type_mismatch",
                    f"槽位 {slot} 只能注入角色定妆图，当前文件不是本项目任何角色的定妆图",
                    slot_key=slot,
                ))
            elif owner.id not in subject_ids:
                issues.append(_issue(
                    "identity_reference_leak",
                    f"角色『{owner.name}』不是本镜主体角色，禁止作为身份参考注入 {slot}",
                    slot_key=slot, entity_id=owner.id,
                ))
        elif slot in ("ref_prev_frame", "first_frame"):
            if asset is None:
                if not _is_continuity_source_frame(db, shot, str(path)):
                    issues.append(_issue(
                        "reference_asset_type_mismatch",
                        f"槽位 {slot} 只能放上一镜实际尾帧，当前文件未登记为尾帧资产",
                        slot_key=slot,
                    ))
                continue
            if asset.asset_type != "last_frame":
                issues.append(_issue(
                    "reference_asset_type_mismatch",
                    f"槽位 {slot} 只能对应尾帧资产，实际为 {asset.asset_type}",
                    slot_key=slot, asset_id=asset.id,
                ))
                continue
            if slot == "ref_prev_frame":
                prev = _previous_shot(db, shot)
                if prev is not None and prev.last_frame_asset_id and asset.id != prev.last_frame_asset_id:
                    issues.append(_issue(
                        "reference_slot_mismatch",
                        "槽位 ref_prev_frame 只能使用上一镜实际尾帧，不能替换为其他尾帧",
                        slot_key=slot, asset_id=asset.id,
                    ))
        elif slot == "ref_image" and asset is not None:
            if asset.asset_type != "keyframe":
                issues.append(_issue(
                    "reference_asset_type_mismatch",
                    f"槽位 ref_image 应放当前镜头分镜图，实际为 {asset.asset_type}",
                    slot_key=slot, asset_id=asset.id,
                ))
                continue
            job = db.get(Job, asset.source_job_id) if asset.source_job_id else None
            if job is not None and job.shot_id and job.shot_id != shot.id:
                issues.append(_issue(
                    "reference_slot_mismatch",
                    "槽位 ref_image 注入了其他镜头的分镜图，构图基准会串镜",
                    slot_key=slot, asset_id=asset.id, source_shot_id=job.shot_id,
                ))

    # 同一张图不得承担多个职责：重复绑定会让模型把尾帧当构图/身份用，
    # 也是「上一镜画面叠加到当前画面」的直接成因。
    seen_paths: dict[str, str] = {}
    for slot, path in media.items():
        if not path:
            continue
        key = _norm_path(path)
        if key in seen_paths:
            first = seen_paths[key]
            extra = ""
            if {first, slot} & {"ref_prev_frame", "ref_image"}:
                extra = "；上一镜尾帧不能同时充当本镜分镜图，否则会出现叠化/虚影"
            issues.append(_issue(
                "reference_slot_duplicate",
                f"同一张参考图被同时绑定到 {first} 和 {slot}，两个槽位职责不同，请改用对应资产{extra}",
                slot_key=slot, duplicate_slot=first,
            ))
        else:
            seen_paths[key] = slot
    return issues


def _assert_unique_slots(slots: list[dict], blocking: list[dict]) -> None:
    seen: set[str] = set()
    for slot in slots:
        key = slot["slot_key"]
        if key in seen:
            blocking.append(_issue("reference_slot_duplicated", "参考图槽位重复", slot_key=key))
        seen.add(key)


def _build_position_specs(shot: Shot, characters: list, scene: Scene | None) -> tuple[str, str, str, str]:
    """构建结构化角色站位数据，返回 (position_specs, standable_items, opening_blocking, gaze_targets)。

    优先从 shot.start_state 读取已有站位；无数据时根据角色列表生成兜底文本。
    start_state 期望结构：{"positions": {"角色名": {"item": "...", "side": "...",
    "along": "...", "posture": "站/坐", "facing": "..."}}, "standable_items": [...],
    "blocking": "开场走位句"}
    """
    start_state = shot.start_state or {}
    positions = start_state.get("positions", {}) if isinstance(start_state, dict) else {}
    standable_raw = start_state.get("standable_items", []) if isinstance(start_state, dict) else []
    opening_blocking = start_state.get("blocking", "") if isinstance(start_state, dict) else ""

    if positions and opening_blocking:
        lines: list[str] = []
        for c in characters:
            pos = positions.get(c.name) if isinstance(positions, dict) else None
            if pos and isinstance(pos, dict):
                lines.append(
                    f"- {c.name}：{pos.get('posture', '站')}于 {pos.get('item', '场景内')}"
                    f"的{pos.get('side', '')}侧"
                    f"{'，沿该侧 ' + pos.get('along', '') if pos.get('along') else ''}"
                    f"，面朝{pos.get('facing', '前方')}"
                )
        position_specs = "\n".join(lines) if lines else "按已审核参考图与镜头动作安排各角色位置"
        standable_list = [
            f"{s.get('name', '?')}" + ("（可坐）" if s.get('can_sit') else "")
            for s in standable_raw
        ] if isinstance(standable_raw, list) else []
        standable_items = "、".join(standable_list) if standable_list else "使用场景基准图中所有固定结构"
        # 视线落点：从对白 segments 判断说话人与听话人
        segs = shot.dialogue_segments or []
        speaker_names = set()
        for seg in segs:
            if isinstance(seg, dict):
                speaker_names.add(seg.get("speaker", ""))
        if len(characters) >= 2 and speaker_names:
            gaze_targets = f"{list(speaker_names)[0]} 看对手，其余角色视线跟随对话自然流动"
        else:
            gaze_targets = "单人或空镜，视线按情绪自然落点"
        return position_specs, standable_items, opening_blocking, gaze_targets

    # 扁平结构分支：库中 start_state 实际为六项枚举（pose/position/expression/holding/
    # costume/injury），从不含 positions 键，此前一律落到下面的兜底文本，
    # 多角色镜头因此拿不到任何可区分的站位依据。
    flat_position = ""
    flat_pose = ""
    if isinstance(start_state, dict):
        flat_position = str(start_state.get("position") or "").strip()
        flat_pose = str(start_state.get("pose") or "").strip()
    if characters and (flat_position or flat_pose):
        primary = characters[0].name
        anchor = flat_position or "按分镜图参考图"
        posture = f"，{flat_pose}" if flat_pose else ""
        lines = [f"- {primary}：位于{anchor}{posture}（本镜主角，站位以此为准）"]
        for other in characters[1:]:
            lines.append(
                f"- {other.name}：与 {primary} 同处本镜头画幅内，"
                f"相对方位以分镜图参考图为准，不得与 {primary} 互换左右前后"
            )
        position_specs = "\n".join(lines)
        standable_items = "使用场景基准图中所有固定结构"
        if not opening_blocking:
            opening_blocking = f"{primary} 位于{anchor}{posture}"
        if len(characters) >= 2:
            gaze_targets = f"{primary} 与 {characters[1].name} 对视，或按对白自然流动"
        else:
            gaze_targets = f"{primary} 看对手或按情绪落点"
        return position_specs, standable_items, opening_blocking, gaze_targets
    # 兜底：无 start_state 站位数据时以文字描述代替
    if not characters:
        return "空镜，无角色站位", "无站位物件（空镜）", "空镜，无开场走位", "无（空镜）"
    names = "、".join(c.name for c in characters)
    position_specs = f"{names} 按已审核参考图与镜头动作安排相对位置"
    standable_items = "使用场景基准图中所有固定结构"
    opening_blocking = f"{names} 按剧本设定自然站位"
    gaze_targets = f"{characters[0].name} 看对手或按情绪落点" if len(characters) == 1 else f"{characters[0].name} 说话时看听话人"
    return position_specs, standable_items, opening_blocking, gaze_targets


def _build_character_positions_summary(characters: list) -> str:
    """构建视频提示词中角色空间位置摘要（场景方位表达，不涉及画面左右）。"""
    if not characters:
        return "no characters present"
    if len(characters) == 1:
        return (
            "single character anchored to the scene layout objects depicted in the "
            "storyboard reference image"
        )
    return (
        f"{len(characters)} characters anchored to distinct scene layout objects as depicted in "
        "the storyboard reference image; each character keeps a separate anchor object and must "
        "not exchange anchors or merge into one position"
    )


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
    # 人工审核闸口已取消（2026-11）：资产是否可用由 assets_gate 的产物校验决定，
    # 提示词编译阶段不再因 assets_review_status 阻断。

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
    # 角色槽位：按工作流声明顺序取所有角色槽（ref_identity/ref_character_2/ref_character_N…），
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


    # 道具槽位：Phase 3 扩展为 ref_prop/ref_prop_2 多道具池，工作流不支持时降级文字描述
    # 且显式告警（不静默丢图）；index 越界=能力超限，不阻断仅告警避免整批出图被打断
    prop_slot_pool = ["ref_prop", "ref_prop_2"]
    for index, prop in enumerate(prop_rows):
        asset, issue = _approved_asset(db, prop.anchor_asset_id, project_id, ("prop",))
        if issue:
            blocking.append({**issue, "entity_id": prop.id, "entity_type": "prop"})
            continue
        if asset is None:
            warnings.append(f"prop_asset_missing: 道具 {prop.name} 无审核通过锚定图，仅以文字描述")
            continue
        if index >= len(prop_slot_pool) or prop_slot_pool[index] not in declared_map:
            warnings.append(f"prop_reference_unsupported: 当前工作流无可用道具槽位（slot={index}），道具 {prop.name} 仅文字描述")
            continue
        _append_slot(slots, declared_map, prop_slot_pool[index], entity_id=prop.id, entity_type="prop",
                     asset=asset, role="prop_identity_reference", blocking=blocking,
                     asset_label="道具")

    if limit and len(slots) > limit:
        blocking.append(_issue("reference_capacity_exceeded", "参考图数量超过工作流上限",
                                count=len(slots), limit=limit))
    _assert_unique_slots(slots, blocking)

    style = _style_for(db, project, "storyboard")
    style_snap = style_config.style_snapshot((project.settings if project else {}) or {},
                                             project_id=project.id if project else None)
    # P1 聚合改造：通过 shot_timeline() 访问器读取，旧数据自动回退 beats
    from app.services.shot_timeline import shot_timeline as _tl
    timeline = _tl(shot)
    action = _text(timeline[0] if timeline else (shot.dialogue or ""), "镜头动作未提供")
    position_specs, standable_items, opening_blocking, gaze_targets = _build_position_specs(shot, characters, scene)
    def _image_no(slot: dict, fallback: int) -> int:
        """槽位对应的提示词编号：必须按「实际注入后压缩的连续序号」取值。

        2026-11-24 修复（图序错位）：原实现直接取工作流静态声明的
        ``input_key`` 的 image_N（如道具固定写 ``images.image_6``），但执行器
        ``compact_reference_slots()`` 会删掉未注入的槽位并把剩下的键重排成 0..N-1。
        于是「场景+2角色+道具」实际只注入 4 张图，提示词却把道具标成
        <image6> —— 文字说的「图6」在模型那里根本不存在，道具约束整段失效。
        现在用 reference_layout.compressed_numbers 按真实注入槽位算连续编号，
        与 reference_layout.final_input_key / 页面槽位展示完全同一套数字。
        """
        from app.services.reference_layout import compressed_numbers

        active = [str(s["slot_key"]) for s in slots if s.get("slot_key")]
        numbers = compressed_numbers(active, declared)
        key = slot.get("slot_key")
        if key and key in numbers:
            return numbers[key]
        return fallback

    # ── ref_duty_block：按 _SECTION_RE 兼容格式生成参考图职责块 ──
    scene_slot = next((s for s in slots if s["role"] == "scene_layout_reference"), None)
    _duty_lines: list[str] = []
    if scene_slot is not None:
        _duty_lines.append(
            _DUTY_LINE["scene"].format(n=_image_no(scene_slot, 1), name=scene.title if scene else "未命名场景")
        )
    for character in characters:
        slot = next((s for s in slots
                     if s["entity_id"] == character.id and s["role"].startswith("character")), None)
        if slot is not None:
            _duty_lines.append(
                _DUTY_LINE["character"].format(n=_image_no(slot, 2), name=character.name)
            )
    for prop in prop_rows:
        slot = next((s for s in slots
                     if s["entity_id"] == prop.id and s["role"] == "prop_identity_reference"), None)
        if slot is not None:
            _duty_lines.append(
                _DUTY_LINE["prop"].format(n=_image_no(slot, 6), name=prop.name)
            )
    ref_duty_block = "\n".join(_duty_lines) or "- 无参考图绑定，全部按文字描述生成"
    # ── action_moment：只取起始瞬间，不带 end_pose ──
    b0 = (timeline or [{}])[0] if isinstance(timeline, list) and timeline else {}
    action_text = _text(b0.get("action") if isinstance(b0, dict) else action, "镜头动作未提供")
    start_pose = b0.get("start_pose", "") if isinstance(b0, dict) else ""
    action_moment = (
        f"画面定格在「{action_text}」刚开始的那一瞬间"
        + (f"（起始姿态：{start_pose}）" if start_pose else "")
    )
    # ── aspect_desc：项目画幅中文描述 ──
    # Project 模型只持久化 aspect_ratio，width/height 可能不存在（历史库/精简模型）；
    # 直接取属性会在重新生成链路抛 AttributeError，导致整条出图被中断。
    aspect_desc = _aspect_desc(
        getattr(project, "aspect_ratio", None) if project is not None else None,
        getattr(project, "width", None) if project is not None else None,
        getattr(project, "height", None) if project is not None else None,
    )
    # ── 契约编译（Phase 3）：SHOT_PLAN_COMPILE 开启且 shot_plan 存在时，
    # 站位/姿态/朝向/视线/构图以契约为准，否则保持旧口径（行为不变）──
    plan = shot.shot_plan if isinstance(shot.shot_plan, dict) else None
    plan_chars = (plan or {}).get("on_screen_characters") or []
    plan_comp = (plan or {}).get("composition") or {}
    if plan_comp and not isinstance(plan_comp, dict):
        plan_comp = {}
    _plan_char_map = {pc.get("character_id"): pc for pc in plan_chars if isinstance(pc, dict)}

    contract_enabled = get_settings().shot_plan_compile and plan is not None
    # 构图：契约 composition 字段优先，缺失回退旧 heuristic
    if contract_enabled and plan_comp:
        _contract_composition = "、".join(v for v in [
            plan_comp.get("framing"), plan_comp.get("focus"),
            plan_comp.get("relation_note")
        ] if v) or "主体清晰、空间关系合理，前景中景背景层次分明"
        _contract_shot_size = plan_comp.get("shot_size") or _text(shot.shot_type)
        _contract_camera_angle = plan_comp.get("camera_angle") or _text(shot.camera_angle)
    else:
        _contract_composition = "主体清晰、空间关系合理，前景中景背景层次分明"
        _contract_shot_size = _text(shot.shot_type)
        _contract_camera_angle = _text(shot.camera_angle)

    # 站位/朝向/视线提示：有契约时从 on_screen_characters 提取
    _plan_posture_hints: list[str] = []
    if contract_enabled:
        for c in characters:
            pc = _plan_char_map.get(c.id)
            if not pc:
                continue
            parts: list[str] = [c.name]
            if pc.get("posture"):
                parts.append(f"姿态：{pc['posture']}")
            if pc.get("facing"):
                parts.append(f"朝向：{pc['facing']}")
            if pc.get("gaze_target") and isinstance(pc["gaze_target"], dict):
                gt = pc["gaze_target"]
                if gt.get("target_character_id"):
                    target_char = db.get(Character, gt["target_character_id"])
                    if target_char:
                        parts.append(f"视线：看向{target_char.name}")
                elif gt.get("target_prop_id"):
                    target_prop = db.get(Prop, gt["target_prop_id"])
                    if target_prop:
                        parts.append(f"视线：看向{target_prop.name}")
                elif gt.get("description"):
                    parts.append(f"视线：{gt['description']}")
            if pc.get("action_at_start"):
                parts.append(f"首帧动作：{pc['action_at_start']}")
            _plan_posture_hints.append("；".join(parts))
    _plan_posture_block = "\n".join(_plan_posture_hints) if _plan_posture_hints else ""

    mapping = {
        "shot_id": shot.id,
        "characters": "、".join(c.name for c in characters) or "无（空镜）",
        "character_count": len(characters),
        # 人数约束必须是可判定的自然语句
        "character_count_rule": (
            "画面中不得出现任何人物，也不出现人物剪影、背影、人体局部或人群中的人"
            if not characters else
            f"画面中的人物总数必须严格等于 {len(characters)} 人（{'、'.join(c.name for c in characters)}）"
            "，不多不少；每个人只出现一次，不出现分身、镜像、重影、背影复制"
        ),
        "scene": scene.title if scene is not None and scene.title else "未指定场景",
        "props": "、".join(p.name for p in prop_rows) or "无",
        "action_moment": action_moment,
        "aspect_desc": aspect_desc,
        "ref_duty_block": ref_duty_block,
        "position_specs": position_specs,
        "standable_items": standable_items,
        "opening_blocking": opening_blocking,
        "gaze_targets": gaze_targets,
        "plan_posture_block": _plan_posture_block,
        "prop_interaction": "道具只按镜头绑定关系出现在角色手中或环境里",
        "emotion": shot.emotion or "neutral",
        "shot_size": _contract_shot_size,
        "camera_angle": _contract_camera_angle,
        "composition": _contract_composition,
        "camera_movement": _text(shot.camera_movement, "固定机位"),
        "lighting": (
            "光从左上方照下，投影落向右下，与场景板主光方向一致；"
            "高光柔和不过曝，阴影保留细节不死黑，对比适中，色彩饱和度与场景板一致"
        ),
        "style": style,
    }

    meta = TEMPLATE_REGISTRY[TEMPLATE_STORYBOARD]
    return CompiledPrompt(
        task_type=TASK_STORYBOARD_IMAGE,
        template_id=TEMPLATE_STORYBOARD,
        template_version=int(meta["version"]),
        content_hash=_template_hash(meta),
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


def resolve_keyframe_prompt(
    db: Session,
    shot: Shot,
    wf: Workflow | None = None,
    *,
    plan_prompt: str = "",
    user_edited: bool = False,
) -> tuple[str, dict]:
    """分镜图提示词单一出口：所有入口（批量/单镜/重生成/Agent）统一走这里。

    - 用户手改 → 原样使用
    - 确定性编译成功 → 用 compile_storyboard_image 渲染的 v2 模板
    - 编译被阻断（资产未就绪等） → 回退 plan_prompt，但补参考图职责段

    返回 (prompt, meta)。meta.source ∈ {user_edited, compiled, fallback}。
    """
    if user_edited or (plan_prompt and plan_prompt.strip()):
        # 无法确认是否手改时的保守策略：已有非空提示词视为已确认，不覆盖。
        # Step 3（prompt_user_edited 字段）落地后此处改为只对真正手改的保留。
        if user_edited:
            return plan_prompt, {"source": "user_edited"}
    cp = compile_storyboard_image(db, shot.id, workflow=wf)
    if not cp.blocking_issues and cp.positive_prompt.strip():
        return cp.positive_prompt, {
            "source": "compiled",
            "template_version": cp.template_version,
            "template_id": cp.template_id,
        }
    # 回退：用 plan_prompt，补参考图职责段
    fallback = (plan_prompt or shot.image_prompt or shot.shot_purpose or "").strip()
    return fallback, {
        "source": "fallback",
        "blocking": cp.blocking_issues,
    }


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

    Shot.seq 是场景内镜号，跨场景比较必须先比 Scene.seq 再比 Shot.seq，
    否则会把后续场景里镜号更小的镜头误判成「更早」。
    """
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return False
    return (
        db.query(Shot.id)
        .join(Scene, Scene.id == Shot.scene_id)
        .filter(
            Scene.project_id == scene.project_id,
            Shot.episode == shot.episode,
            or_(
                Scene.seq < scene.seq,
                and_(Scene.seq == scene.seq, Shot.seq < shot.seq),
            ),
        )
        .first()
    ) is not None


def _build_screen_geography(characters: list, *, reference_bound: bool = True) -> str:
    """生成屏幕空间地理描述：多人镜头强调互相对视与分镜图站位锁定。

    H3 多人对话常见问题是角色堆在画面中央、左右互换、说话时不看对方，
    这里把「谁在左/谁在右、互相对视、说话人转向听话人」写成显式约束。
    """
    if not characters:
        return ""
    # 无参考图绑定时不能再断言「以分镜图为准」，否则与 Reference bindings 的
    # 「仅凭文字生成」直接冲突。站位锚点随绑定情况切换措辞。
    anchor = "the storyboard reference image" if reference_bound else "the described text layout"
    if len(characters) == 1:
        return f"{characters[0].name} stays in the exact position defined by {anchor}."
    # 多人：按角色出场顺序对应 S1/S2，描述互相对视与站位保持
    names = [c.name for c in characters]
    first, second = names[0], names[1]
    others = ", ".join(names[2:]) if len(names) > 2 else None
    base = (
        f"{first} and {second} face each other and maintain their respective "
        f"left/right positions from {anchor}; the speaking "
        "character turns toward the listener, and the listener looks at the speaker."
    )
    if others:
        base += f" {others} keep their respective positions and look toward the conversation."
    return base


def _build_shot_dialogue(shot: Shot, characters: list) -> str:
    """构建带稳定说话人编号的对白字符串。

    - 优先用 dialogue_segments（按 character_id 匹配出场顺序分配 S1/S2/...）
    - 回退到 shot.dialogue 纯文本时统一用 (S1)
    - 对白必须用 <d>[Chinese] 文本</d> 包裹（MiniMax H3 官方格式）
    """
    segs = [s for s in (shot.dialogue_segments or [])
            if isinstance(s, dict) and (s.get("text") or "").strip()]
    if segs:
        parts: list[str] = []
        for seg in segs:
            cid = seg.get("character_id") or seg.get("speaker_id")
            speaker_idx = next((i for i, c in enumerate(characters) if c.id == cid), 0)
            text = seg["text"].strip().replace("\n", " ")
            parts.append(f"(S{speaker_idx + 1}) <d>[Chinese] {text}</d>")
        return " ".join(parts)
    if (shot.dialogue or "").strip():
        text = shot.dialogue.strip().replace("\n", " ")
        return f"(S1) <d>[Chinese] {text}</d>"
    return "no dialogue"


# 『对白与声音』分段定位（兼容 【对白与声音】/对白与声音：/【环境声】三种写法）
_DIALOGUE_SECTION_RE = re.compile(r"【?对白与声音】?\s*[:：]?|【环境声】\s*[:：]?")
# 声音描述关键词：从历史段落里仅保留这类句子，避免台词与标签一起被朗读
_SOUND_KEYWORDS = ("环境声", "背景音", "音效", "音乐", "BGM", "bgm", "氛围", "人群声", "现场声", "白噪音")
# 历史对白写法里的引号台词：“……” / "……"
_QUOTE_RE = re.compile(r"[“\"]([^”\"]*)[”\"]")
_NO_VOICE_LINE = "无任何人声对白、旁白或口型动作"


def _h3_dialogue_text(shot: Shot) -> str:
    """按结构化 dialogue_segments 生成 H3 播报串：只有 <d> 内是台词。"""
    segs = [
        s for s in (shot.dialogue_segments or [])
        if isinstance(s, dict) and (s.get("text") or "").strip()
    ]
    if not segs:
        text = (shot.dialogue or "").strip().replace("\n", " ")
        return f"(S1) says: <d>[Chinese] {text}</d>" if text else ""
    order: list[str] = []
    for seg in segs:
        key = str(seg.get("character_id") or seg.get("speaker_id") or seg.get("speaker") or "")
        if key and key not in order:
            order.append(key)
    parts: list[str] = []
    for seg in segs:
        key = str(seg.get("character_id") or seg.get("speaker_id") or seg.get("speaker") or "")
        index = order.index(key) + 1 if key in order else 1
        text = seg["text"].strip().replace("\n", " ")
        # 姓名不写进对白行：H3 会把「姓名 (S1) says:」里的姓名当成台词念出来，
        # 且容易出现念错音；说话人身份由【身份约束】段承担，这里只保留 (Sx) ID
        parts.append(f"(S{index}) says: <d>[Chinese] {text}</d>")
    return " ".join(parts)


def _soundscape_hint(block: str, shot: Shot) -> str:
    """从原『对白与声音』段里取出环境声/音乐描述，取不到就返回空。"""
    value = (shot.soundscape or "").strip()
    if value:
        return value
    quotes_end = max(block.rfind("”"), block.rfind('"'))
    tail = block[quotes_end + 1:] if quotes_end >= 0 else block
    hits = [tail.find(k) for k in _SOUND_KEYWORDS if k in tail]
    if not hits:
        return ""
    text = tail[min(hits):].strip().strip("；;。 ")
    # “背景音乐”会被“背景音”这类短关键词截成“乐”，长度异常时退回整段
    return text if len(text) > 2 else tail.strip().strip("；;。 ")


def _speaker_order(segs: list[dict]) -> list[str]:
    order: list[str] = []
    for seg in segs:
        key = str(seg.get("character_id") or seg.get("speaker_id") or seg.get("speaker") or "")
        if key and key not in order:
            order.append(key)
    return order


def _tagged_dialogue_block(block: str, shot: Shot) -> str:
    """按结构化台词就地改写历史『对白与声音』段。

    历史写法「@角色 台词：第1段“……”语气……」→
    「(S1) <d>[Chinese] ……</d>」，只把引号台词换成 <d> 标签并删掉
    「台词/第N段/@角色」标签；<d> 之外的语气说明由 _strip_tone_after_tag
    统一剥除，避免被当成台词念出；已是 <d> 格式的段落原样返回，保证幂等。
    """
    if "<d>" in block:
        # 已有 <d> 的旧提示词补上官方 says 语法，保证 <d> 是唯一播报源
        return _ensure_says_before_tag(block, shot)
    segs = [
        s for s in (shot.dialogue_segments or [])
        if isinstance(s, dict) and (s.get("text") or "").strip()
    ]
    if not segs:
        return ""
    if not _QUOTE_RE.search(block):
        return _h3_dialogue_text(shot)
    order = _speaker_order(segs)
    rebuilt = block
    for seg in segs:
        key = str(seg.get("character_id") or seg.get("speaker_id") or seg.get("speaker") or "")
        index = order.index(key) + 1 if key in order else 1
        text = seg["text"].strip().replace("\n", " ")
        match = _QUOTE_RE.search(rebuilt)
        tagged = f"(S{index}) says: <d>[Chinese] {text}</d>"
        if match is None:
            rebuilt = f"{rebuilt} {tagged}"
            continue
        rebuilt = f"{rebuilt[:match.start()]}{tagged}{rebuilt[match.end():]}"
    # 清掉说话人/段落标签：H3 只认 <d>，标签留在段外会被当作描述甚至念出来
    rebuilt = re.sub(r"@[^\s，。；、：:]+", "", rebuilt)
    rebuilt = re.sub(r"(?:台词|对白)\s*[:：]", "", rebuilt)
    rebuilt = re.sub(r"第[0-9一二三四五六七八九十]+段\s*", "", rebuilt)
    rebuilt = re.sub(r"^\s*[；;，,。]\s*", "", rebuilt)
    rebuilt = re.sub(r"[；;]\s*[；;]+", "；", rebuilt)
    rebuilt = re.sub(r"\s{2,}", " ", rebuilt)
    return rebuilt.strip().strip("；; ")


def _sound_only(block: str) -> str:
    """无对白镜头：从原段落里剔除“无对白/没有对白”等字样，只保留声音描述。"""
    text = re.sub(r"^\s*[；;。、,，]*\s*(?:无对白|没有对白|无台词|no\s+dialogue)\s*", "", block, flags=re.I)
    text = re.sub(r"^(?:对白|台词)\s*[:：]\s*", "", text)
    return text.strip().strip("；;。 ")


# 「【输出】时长5秒，16:9横画幅，24fps」这类技术规格行：画幅、帧数与时长
# 由工作流节点参数（width/height/length）直接注入，写进提示词反而会与项目
# 实际画幅冲突（例如项目 9:16 却在提示词里写 16:9），统一剥掉
_OUTPUT_SPEC_RE = re.compile(r"(?m)^[ \t]*(?:【输出】|输出\s*[:：])[^\n]*\n?")


def _strip_output_spec(text: str) -> str:
    """剥掉提示词里的输出规格行（时长/画幅/帧率），只保留画面与声音描述。"""
    cleaned = _OUTPUT_SPEC_RE.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()


# 对白标签之后的表演提示（如「</d>语气温和；」）会被 H3 当作台词念出来，
# 语气/情绪只保留在结构化 dialogue_segments.emotion 与画面表情描述里，提示词必须剥掉
_TONE_AFTER_TAG_RE = re.compile(
    r"</d>\s*[，,、；;：:]?\s*"
    r"[（(]?\s*(?:以|用)?(?:语气|语调|口吻|嗓音|声音|语速|情绪)"
    r"(?:(?!环境声|音效|背景音|人声|旁白)[^；;。\n<）)]){0,24}"
    r"(?:[）)]?[；;。]|[）)]|(?=\s*(?:\(S\d\)|<d>|环境声|音效|背景音|人声|旁白|$)))"
)


def _strip_tone_after_tag(text: str) -> str:
    """剥掉 <d> 标签后的语气说明，避免模型把“语气温和”当台词念出来。"""
    cleaned = _TONE_AFTER_TAG_RE.sub("</d>", text or "")
    return re.sub(r"</d>[ \t]*?(?=\(S\d\))", "</d> ", cleaned)


# H3 官方对白语法是「(S1) says: <d>[Chinese] 台词</d>」；只有 (S1) 而没有 says 时，
# 模型不把 <d> 当作唯一播报来源，容易自行补台词
_SPEAKER_BEFORE_TAG_RE = re.compile(r"\((S\d)\)(?:\s*says)?\s*[:：]?\s*(?=<d>)")
# 对白行里写在 (Sx) 前面的角色姓名会被 H3 当成台词念出来（常念错音），
# 说话人身份由【身份约束】段承担，这里把姓名剥掉
_NAME_BEFORE_SPEAKER_RE = re.compile(
    r"(?:^|(?<=[\s，。；：、]))"
    r"[^\s，。；：、<>《》()（）]{1,10}[ \t]+"
    r"(?=\((?:S\d)(?:\s*,\s*S\d)*\)\s*(?:says)?\s*[:：]?\s*<d>)"
)


def _ensure_says_before_tag(text: str, shot: Shot | None = None) -> str:
    """收敛对白行：「姓名 (Sx) says: <d>」→「(Sx) says: <d>」，姓名不参与播报。"""
    cleaned = _NAME_BEFORE_SPEAKER_RE.sub("", text or "")
    return _SPEAKER_BEFORE_TAG_RE.sub(lambda m: f"({m.group(1)}) says: ", cleaned)


# 有对白镜头的动作描述里只要出现「抱怨」「开口」「问话」这类说话行为，
# H3 就会在 <d> 台词之外再自创一段角色台词（用户听到的“莫名其妙台词”）。
# 说话行为只由 <d> 表达，这些表述统一改写成不带发声的神情/视线描述。
_SPEECH_ACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"问出[^，。；;、\n]{0,10}"), "露出疑惑神情"),
    (re.compile(r"(?:喃喃自语|自言自语|嘟囔|嘀咕|念叨|吐槽)[^，。；;、\n]{0,10}"), "垂下眼"),
    (re.compile(r"(?<![不别勿禁没])抱怨[^，。；;、\n]{0,10}"), "面露无奈"),
    (re.compile(r"(?<![不别勿禁没])(?:扬声|喊叫|大喊|尖叫|嚷)[^，。；;、\n]{0,10}"), "眉头一紧"),
    (re.compile(r"(?:说道|说出|说：|说:)[^，。；;、\n]{0,10}"), "看向对方"),
    (re.compile(r"(?<![不别勿禁没])(?:回答|答道|回应|反问)"), "点头"),
    (re.compile(r"问话"), "蹙眉"),
    (re.compile(r"(?<![不别勿禁没])开口"), "抿唇"),
)
_D_TAG_SPLIT_RE = re.compile(r"(<d>.*?</d>)", re.S)


def _despine_speech_actions(text: str) -> str:
    """改写动作描述里的说话类表述，避免 H3 自创 <d> 之外的台词。

    <d></d> 内的台词原文不动；返回结果可能出现“并面露无奈”这类拼接，
    因此顺带收敛重复标点。
    """
    parts = _D_TAG_SPLIT_RE.split(text or "")
    for i, part in enumerate(parts):
        if part.startswith("<d>"):
            continue
        for pattern, repl in _SPEECH_ACTION_RULES:
            part = pattern.sub(repl, part)
        part = re.sub(r"[，,]{2,}", "，", part)
        part = re.sub(r"([。；;])[，,、]*", r"\1", part)
        parts[i] = part
    return "".join(parts)


# 显式约束：H3 只播报 <d> 内的台词且只播一次，<d> 之外不得出现人声
_DIALOGUE_LOCK_LINE = (
    "【台词约束】只播报 <d></d> 内的台词，每句仅一次；"
    "不得新增、改写、重复或自创任何台词、旁白、人声，"
    "也不得念出角色姓名、称呼或说话人标签，<d> 之外不出现说话声。"
)


def _ensure_dialogue_lock(text: str) -> str:
    """追加/刷新台词约束行（旧版本留下的约束行就版本地替换，避免重复叠加）。"""
    if re.search(r"(?m)^[ \t]*【台词约束】", text or ""):
        return re.sub(r"(?m)^[ \t]*【台词约束】[^\n]*", _DIALOGUE_LOCK_LINE, text, count=1)
    return f"{text.rstrip()}\n{_DIALOGUE_LOCK_LINE}"


# H3 三段式里的声音字段起点：画面描述与声音字段需分开处理
_SOUNDSCAPE_LINE_RE = re.compile(r"(?m)^[ \t]*(?:overall_soundscape|non_diegetic_music)\s*[:：]")


def _split_sound_fields(text: str) -> tuple[str, str]:
    """按第一个声音字段行把提示词切成（画面描述, 声音字段）。"""
    match = _SOUNDSCAPE_LINE_RE.search(text or "")
    if match is None:
        return text or "", ""
    return text[:match.start()], text[match.start():]


def normalize_clip_dialogue(shot: Shot, prompt: str) -> str:
    """把分镜视频提示词的『对白与声音』段改成 H3 可播报格式。

    历史模板与解析产物常写成「@角色 台词：第1段“……”语气……」，
    H3 会把「台词」「对白」「第N段」等标签一起念出来。这里以结构化
    dialogue_segments 为唯一播报来源重建该段：<d> 内只有台词原文，
    标签与声音描述留在 <d> 外；<d> 之外的语气说明一并剥除。
    """
    text = _ensure_says_before_tag(_strip_tone_after_tag(_strip_output_spec(prompt or "")), shot)
    if not text.strip():
        return text
    dialogue = _h3_dialogue_text(shot)
    match = _DIALOGUE_SECTION_RE.search(text)
    if match is None:
        # 没有分段时：已有 <d> 保持原样；否则把对白单独补成可播报段
        if "<d>" in text or not dialogue:
            return text
        return f"{text}\n\n【对白与声音】{dialogue}"
    rest = text[match.end():]
    end = re.search(r"\n\s*(?:【|输出\s*[:：])", rest)
    block = rest[:end.start()] if end else rest
    tail_text = rest[end.start():] if end else ""
    if not dialogue:
        # 无对白镜头：去掉“无对白/对白”这类字样，只留环境声描述，
        # 否则 H3 会按对白段的口吻凭空生成人声
        sound = _sound_only(block) or (shot.soundscape or "").strip()
        head = text[:match.start()]
        if not sound:
            result = f"{head}{tail_text}".strip()
        else:
            result = f"{head}【环境声】{sound}{tail_text}"
        # 再明确禁止人声，避免模型把声音描述理解成对白去配音
        if "【禁止】" in result:
            if _NO_VOICE_LINE in result:
                return result
            return re.sub(
                r"(【禁止】)([^\n]*)",
                lambda m: f"{m.group(1)}{m.group(2).rstrip('。；; ')}；{_NO_VOICE_LINE}",
                result,
                count=1,
            )
        return f"{result}【禁止】{_NO_VOICE_LINE}。"
    rebuilt = _tagged_dialogue_block(block, shot)
    if not rebuilt:
        sound = _soundscape_hint(block, shot)
        rebuilt = dialogue
        if sound:
            if any(k in sound for k in ("环境声", "背景音", "音效", "音乐", "氛围")):
                rebuilt = f"{rebuilt} {sound}"
            else:
                rebuilt = f"{rebuilt} 环境声：{sound}"
    return f"{text[:match.start()]}【对白与声音】{rebuilt}{tail_text}"


_H3_HEADER = "integrated_multimodal_description:"
_SOUND_PREFIX_RE = re.compile(r"^(?:环境声|背景音|音效|音乐|BGM)\s*(?:为|是|：|:)?\s*")


def _clip_soundscape(block: str, shot: Shot) -> str:
    """从历史声音段里提取纯音效描述（去掉“环境声：”等前缀）。"""
    value = (shot.soundscape or "").strip()
    if value:
        return value
    text = block
    # 段里含 <d> 对白时，声音描述在最后一句台词之后：先丢掉台词与语气，
    # 否则整段对白会被当成 overall_soundscape 再播报一遍
    if "</d>" in text:
        text = text[text.rfind("</d>") + len("</d>"):]
        # 按整句保留含声音关键词的描述，不能从“音乐”这类词中间截断，
        # 否则「旋转木马的音乐与转盘机械声」会变成「与转盘机械声」
        sentences = [s.strip() for s in re.split(r"[。；;！？!?\n]", text) if s.strip()]
        text = "；".join(s for s in sentences if any(k in s for k in _SOUND_KEYWORDS))
    # 该段可能已是纯声音描述（【环境声】之后），只需剥掉标签前缀，
    # 不能再按关键词截断：“背景音乐”会被“背景音”截成“乐”
    text = text.strip().strip("；;。、,， ")
    text = _SOUND_PREFIX_RE.sub("", text).strip().strip("；;。 ")
    if text:
        return text
    if not _h3_dialogue_text(shot):
        return _sound_only(block)
    return ""


def to_h3_clip_prompt(shot: Shot, prompt: str) -> str:
    """把分镜视频提示词整理成 MiniMax H3 规定的三段式结构。

    H3 只从 integrated_multimodal_description / overall_soundscape /
    non_diegetic_music 读取画面、声音与音乐。历史中文分段写法
    （【对白与声音】【环境声】）直接下发时，对白与音效不一定会被模型采纳，
    表现为生成的音轨不按提示词说话。这里保留原有画面描述，把对白收敛为
    (Sx) <d>[Chinese] 台词</d>，声音与音乐分别落到官方字段。
    """
    text = normalize_clip_dialogue(shot, prompt or "")
    if not text.strip():
        return text
    if _H3_HEADER in text:
        text = _strip_output_spec(text)
        # 有对白的镜头：动作描述里的说话类表述会被 H3 演成额外台词，
        # 统一改写并在画面描述末尾补一条“只播 <d> 内台词”的约束
        if _h3_dialogue_text(shot):
            head, sound_fields = _split_sound_fields(text)
            text = _ensure_dialogue_lock(_despine_speech_actions(head)) + sound_fields
        # 已是三段式的提示词也要补齐声音字段：缺 overall_soundscape 时
        # H3 没有任何环境音约束，会用自创人声/台词填满整段时长
        if not re.search(r"(?m)^\s*overall_soundscape\s*[:：]", text):
            sound = _clip_soundscape("", shot) or "场景环境声与动作音效，与画面动作同步"
            text = f"{text.rstrip()}\n\noverall_soundscape: {sound}"
        if not re.search(r"(?m)^\s*non_diegetic_music\s*[:：]", text):
            text = f"{text.rstrip()}\n\nnon_diegetic_music: N/A"
        return text
    dialogue = _h3_dialogue_text(shot)
    sound = ""
    match = _DIALOGUE_SECTION_RE.search(text)
    if match is not None:
        rest = text[match.end():]
        end = re.search(r"\n\s*(?:【|输出\s*[:：])", rest)
        block = rest[:end.start()] if end else rest
        tail_text = rest[end.start():] if end else ""
        sound = _clip_soundscape(block, shot)
        body = f"{text[:match.start()]}{tail_text}".strip()
    else:
        body = text.strip()
    if dialogue:
        body = _despine_speech_actions(body)
        body = f"{body}\n{dialogue}" if body else dialogue
        body = _ensure_dialogue_lock(body)
    else:
        body = f"{body}\nDialogue: no dialogue." if body else "Dialogue: no dialogue."
    return (
        f"{_H3_HEADER}\n{body}\n\n"
        f"overall_soundscape: {sound or '场景环境声与动作音效，与画面动作同步'}\n\n"
        f"non_diegetic_music: N/A"
    )


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

    # 逐镜人工确认（keyframe_confirmed）已取消：视频生成只看是否存在当前有效分镜图，
    # 下面 storyboard_asset_missing / storyboard_file_missing 已覆盖该判定。

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

    # --- 参考图槽位注入：角色一致性 + 分镜图底图 + 道具 ---
    # ref2va 工作流支持 ref_identity/ref_character_2/ref_character_N/ref_image/ref_prop，
    # 但此前 compile_shot_video 只接了 ref_prev_frame，角色/场景/道具/分镜图全部未注入，
    # 导致 H3 生成视频时角色身份仅靠文字描述，一致性无法保证。
    from app.services.ref_inject import is_character_slot
    _declared = _declared_slots(caps)
    character_slot_pool = [item["param"] for item in _declared
                           if is_character_slot(item["param"])]
    if characters and not character_slot_pool:
        warnings.append(
            "character_reference_unsupported: 当前视频工作流无角色参考槽位，角色仅以文字描述呈现"
        )
    for index, character in enumerate(characters):
        asset, issue = _approved_asset(db, character.portrait_asset_id, project_id, ("character",))
        if issue:
            blocking.append({**issue, "entity_id": character.id, "entity_type": "character"})
            continue
        if asset is None:
            blocking.append(_issue("character_portrait_missing",
                                    "角色缺少已审核的定妆图，无法作为视频参考图",
                                    entity_id=character.id))
            continue
        if character_slot_pool and index >= len(character_slot_pool):
            blocking.append(_issue(
                "reference_capacity_exceeded",
                "镜头角色数量超过当前视频工作流参考图槽位能力",
                capacity=len(character_slot_pool),
            ))
            continue
        if not character_slot_pool:
            continue
        _append_slot(slots, declared_map, character_slot_pool[index], entity_id=character.id,
                     entity_type="character", asset=asset, role="character_identity_reference",
                     blocking=blocking, asset_label="角色")

    # 分镜图作为 ref_image 注入（场景结构 + 角色位置 + 构图的权威来源）
    if storyboard is not None and _media_file_exists(storyboard.file_path):
        if "ref_image" not in declared_map:
            warnings.append("storyboard_reference_unsupported: 当前视频工作流无 ref_image 槽位，分镜图仅作为文字参考")
        else:
            _append_slot(slots, declared_map, "ref_image", entity_id=shot.id,
                         entity_type="keyframe", asset=storyboard, role="storyboard_reference",
                         blocking=blocking, asset_label="分镜图")

    # 道具槽位
    for prop in prop_rows:
        asset, issue = _approved_asset(db, prop.anchor_asset_id, project_id, ("prop",))
        if issue:
            blocking.append({**issue, "entity_id": prop.id, "entity_type": "prop"})
            continue
        if asset is None:
            warnings.append(f"prop_asset_missing: 道具 {prop.name} 无审核通过锚定图，仅以文字描述")
            continue
        if "ref_prop" not in declared_map:
            warnings.append(f"prop_reference_unsupported: 当前视频工作流无 ref_prop 槽位，道具 {prop.name} 仅文字描述")
            continue
        _append_slot(slots, declared_map, "ref_prop", entity_id=prop.id, entity_type="prop",
                     asset=asset, role="prop_identity_reference", blocking=blocking,
                     asset_label="道具")

    video_limit = _slot_limit(caps, _declared)
    if video_limit and len(slots) > video_limit:
        blocking.append(_issue("reference_capacity_exceeded",
                                "视频参考图数量超过工作流上限",
                                count=len(slots), limit=video_limit))
    _assert_unique_slots(slots, blocking)

    # 构建参考绑定文字描述（注入提示词，让 H3 知道每张参考图的用途）
    # 按实际绑定的参考图类型切换提示词骨架（T2VA / I2VA / Ref2VA）：
    # 只有分镜图才能定义构图与空间，只有参考图才能锁定身份；
    # 无任何参考图时禁止断言「identity is locked by reference images only」。
    has_storyboard_ref = any(s["role"] == "storyboard_reference" for s in slots)
    has_identity_ref = any(s["role"] == "character_identity_reference" for s in slots)
    ref_parts: list[str] = []
    for s in slots:
        if s["role"] == "character_identity_reference":
            ref_parts.append(
                "Character identity locked by reference image: face, body, costume and colors must match exactly"
            )
        elif s["role"] == "storyboard_reference":
            ref_parts.append(
                "Storyboard reference image defines scene structure, character positions and composition"
            )
        elif s["role"] == "prop_identity_reference":
            ref_parts.append(
                "Prop reference image defines shape, color and material"
            )
        elif s["role"] == "previous_shot_last_frame":
            # 只做时序/空间连续性参考。旧措辞「match lighting, position and state」会被模型
            # 理解成把上一镜融进当前画面，是"尾帧虚影/叠化"的主要来源。
            content_source = (
                "All visible content must come from the storyboard reference image"
                if has_storyboard_ref else
                "All visible content must come from the textual description below"
            )
            ref_parts.append(
                "Previous shot last frame is temporal/spatial continuity reference only — "
                "never reproduce, overlay, blend, double-expose or use it as the current shot's "
                "first frame or background; no ghosting, translucent silhouettes or residue from it. "
                f"{content_source}"
            )
    ref_bindings = "; ".join(ref_parts) or "No reference images bound, generate from text description only"
    if has_storyboard_ref:
        continuity_lock = (
            "This video must continue the approved storyboard reference image: keep every character's "
            "identity, face, body, costume, colors, props and scene structure exactly consistent in "
            "every frame — character identity is locked by reference images only, do not generate any "
            "character based on name."
        )
    elif has_identity_ref:
        continuity_lock = (
            "Keep every character's identity, face, body, costume and colors exactly consistent with "
            "the bound reference images in every frame — do not generate any character based on name. "
            "Scene structure must stay coherent with the textual description."
        )
    else:
        continuity_lock = (
            "No reference image is bound to this shot: generate every character strictly from the "
            "textual description, keep face, body, costume and colors consistent throughout the whole "
            "shot, and do not add characters, props or scene elements that are not described."
        )
    position_specs, _standable_items, _opening_blocking, _gaze_targets = _build_position_specs(
        shot, characters, scene
    )
    screen_geography = _build_screen_geography(characters, reference_bound=has_storyboard_ref)
    if has_storyboard_ref:
        spatial_lock = (
            "Spatial layout is derived from the storyboard reference image: keep every character's "
            "exact position relative to scene objects, facing direction, depth and distance — do not "
            "swap positions, do not re-center, do not move characters closer together or farther apart."
        )
    else:
        spatial_lock = (
            "No storyboard reference image is bound to this shot: derive the spatial layout strictly "
            "from the textual description below, keeping each character in a stable, distinct position "
            "for the whole shot."
        )

    # P1: shot_timeline accessor for timeline->beats fallback
    from app.services.shot_timeline import shot_timeline as _tl
    mapping = {
        "shot_id": shot.id,
        "duration": f"{duration:g}",
        # 模板里已写死 “characters present”，此处只给数字，避免 “2 characters characters present”
        "character_count": f"{len(characters)}" if characters else "0",
        "scene": scene.title if scene is not None and scene.title else "未指定场景",
        "props": "、".join(p.name for p in prop_rows) or "无",
        "start_state": _describe_state(shot.start_state, "延续已审核分镜图初始状态"),
        "main_action": _describe_beats(_tl(shot), "按已审核分镜图完成镜头动作"),
        "end_state": _describe_state(shot.end_state, "延续动作的自然收束状态"),
        "shot_size": _text(shot.shot_type),
        "camera_angle": _text(shot.camera_angle),
        "camera_movement": _text(shot.camera_movement, "固定机位"),
        "pacing": _text(None, "平稳节奏"),
        "environment_changes": "无场景切换",
        "continuity_in_type": continuity_type,
        "source_shot_id": source_shot_id or "无",
        "source_frame_file_id": source_file_id or "无",
        "continuity_out_state": _text(None, "按镜头动作自然结束"),
        # 对白：按 dialogue_segments 给每个说话人分配稳定编号 (S1)/(S2)，
        # 多人镜头必须让 H3 知道哪句是谁说的，否则角色会堆叠/错位；
        # 对白需用 <d>[Language] 文本</d> 包裹（官方格式要求）
        "dialogue": _build_shot_dialogue(shot, characters),
        # 场景空间描述：基于站位数据的角色空间关系，不涉及画面左右（画面左右由机位推导）
        "character_positions_summary": _build_character_positions_summary(characters),
        # 逐角色站位（来自 start_state 的 position/pose，结合出场角色名单）
        "position_specs": position_specs,
        # 屏幕空间关系：多人镜头强调互相对视、不得左右互换
        "screen_geography": screen_geography,
        # 空间锁定句：按实际参考图绑定情况生成，不再无条件断言分镜图
        "spatial_lock": spatial_lock,
        # 延续锁定句：无参考图时不再断言「延续已审核分镜图」
        "continuity_lock": continuity_lock,
        "soundscape": _text(shot.soundscape, "N/A"),
        "music": "N/A",
        "style": style,
        "ref_bindings": ref_bindings,
    }

    meta = TEMPLATE_REGISTRY[TEMPLATE_VIDEO]
    return CompiledPrompt(
        task_type=TASK_SHOT_VIDEO,
        template_id=TEMPLATE_VIDEO,
        template_version=int(meta["version"]),
        content_hash=_template_hash(meta),
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
