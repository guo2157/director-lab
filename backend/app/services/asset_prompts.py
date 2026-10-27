"""资产纯净化提示词模板（P1）：角色定妆图/场景基准图/道具锚定图的统一结构化生成模板。

设计约定（对应资产纯净化与分镜一致性专项改造）：
- 资产图用于定义固定身份，不讲述剧情：单一主体、干净背景、无文字水印、无拼贴
- 模板按资产类型分段：任务 → 主体定义 → 画面要求 → 必须保持 → 禁止 → 输出
- 负面约束作为提示词分段写入（模型不支持独立负面提示词参数时的实际控制方式），
  若工作流声明 negative_prompt 参数槽位，由调用方将禁止段注入对应槽位
- 模板输出为完整提示词字符串，直接作为生成 prompt 使用
"""

# 统一美术风格占位：项目未配置统一风格时使用中性描述，不虚构具体风格
_DEFAULT_STYLE = "童话绘本写实风格，柔和饱和色彩，温暖光照，细腻质感"
# 模板版本（P4 整改）：变更时同步更新，由调用方写入 input_snapshot 供追溯
ASSET_PROMPTS_VERSION = "2.0"


def _require_identity(*values: str, asset_label: str) -> None:
    """必填变量校验：名称/外观/描述全部为空时明确报错，不静默生成占位提示词。"""
    if not any((v or "").strip() for v in values):
        raise ValueError(
            f"{asset_label}_prompt_missing_identity: "
            f"{asset_label}的名称、外观描述与简介均为空，无法构建资产图提示词"
        )

# 角色资产负面约束（第八节模板）
CHARACTER_NEGATIVE = (
    "第二个人物、重复角色、人物剪影、镜中人物、背景人物、额外肢体、"
    "身体结构错误、裁切主体、杂乱背景、无关物品、手持无关道具、文字、"
    "水印、边框、拼贴、多视图、风格混杂、模糊、严重变形"
)

# 场景资产负面约束
SCENE_NEGATIVE = (
    "人物、动物、人物剪影、人体局部、无关道具、文字、水印、边框、"
    "拼贴、多场景组合、重复建筑、空间结构错误、背景杂乱、严重模糊"
)

# 道具资产负面约束
PROP_NEGATIVE = (
    "多个重复物品、无关物品、人物、手部、杂乱背景、文字、水印、"
    "边框、拼贴、结构变形、主体裁切、模糊"
)


def build_character_prompt(
    name: str,
    appearance: str,
    *,
    description: str = "",
    style: str = "",
) -> str:
    """构建角色定妆图纯净化提示词。

    - 单一角色、全身正面或轻微 3/4 视角、站立自然姿势中性
    - 不手持剧情道具、不与其他角色互动、干净纯色或摄影棚背景
    """
    _require_identity(name, appearance, description, asset_label="角色")
    style = style or _DEFAULT_STYLE
    parts = [f"任务：角色定妆图（角色资产图），定义角色『{name}』的固定视觉身份，不讲述剧情。" if name else "任务：角色定妆图（角色资产图），定义角色的固定视觉身份，不讲述剧情。"]
    identity = appearance or description or (name or "角色")
    parts.append(f"角色定义：{identity}")
    parts.append(f"美术风格：{style}，全图统一。")
    parts.append(
        "画面要求：画面中只出现这一个指定角色；全身正面或轻微四分之三视角；"
        "站立自然，姿势中性；双手、双腿和身体结构完整清晰；主体完整居中，不被裁切。"
    )
    parts.append(
        "必须保持：角色身份特征（物种、年龄感、体型、毛色或肤色、脸型、发型或鬃毛、"
        "耳朵、尾巴、服装与配饰）与角色定义完全一致；背景为干净、统一、低干扰的纯色或简单摄影棚背景。"
    )
    parts.append(
        "禁止：不出现第二个人物、人物剪影、镜中人物或背景人物；不与其他角色互动；"
        "不手持剧情道具（除非该物品是角色固定外观的一部分）；不出现标题、标签、文字、"
        "水印、边框、拼贴或装饰。"
    )
    parts.append("输出：单张完整角色定妆图，主体清晰独立，细节完整。")
    return "\n".join(parts)


def build_scene_prompt(
    title: str,
    description: str,
    *,
    style: str = "",
) -> str:
    """构建场景基准图纯净化提示词。

    - 只定义一个场景，不出现任何角色与独立道具
    - 环境组成部分（地面、植被、水体、建筑结构）允许存在
    """
    _require_identity(title, description, asset_label="场景")
    style = style or _DEFAULT_STYLE
    parts = [f"任务：场景基准图（场景资产图），定义场景『{title}』的环境与空间关系，不出现角色。" if title else "任务：场景基准图（场景资产图），定义场景的环境与空间关系，不出现角色。"]
    identity = f"{title}。{description}" if title and description else (description or title or "场景")
    parts.append(f"场景定义：{identity}")
    parts.append(f"美术风格：{style}，全图统一。")
    parts.append(
        "画面要求：只定义这一个场景；保持明确的空间布局、地面、背景、光照和色彩；"
        "环境组成部分（如地面、草地、树木、河水、河岸、建筑结构）按场景定义呈现；"
        "保持可用于后续镜头构图的空间深度。"
    )
    parts.append(
        "必须保持：环境结构与空间布局与场景定义一致；主色调与光照设定统一。"
    )
    parts.append(
        "禁止：不出现任何人物、动物、人体局部、剪影；不出现不属于场景固定布置的独立道具；"
        "不出现文字、水印、边框、分镜格子、漫画边框或多视角拼贴。"
    )
    parts.append("输出：单张完整场景基准图，空间结构清晰。")
    return "\n".join(parts)


def build_prop_prompt(
    name: str,
    appearance: str,
    *,
    description: str = "",
    style: str = "",
) -> str:
    """构建道具锚定图纯净化提示词。

    - 只出现一个指定道具（或明确规定为整体的道具组合）
    - 主体完整居中，统一干净背景
    """
    _require_identity(name, appearance, description, asset_label="道具")
    style = style or _DEFAULT_STYLE
    parts = [f"任务：道具锚定图（道具资产图），定义道具『{name}』的固定外观，不出现角色。" if name else "任务：道具锚定图（道具资产图），定义道具的固定外观，不出现角色。"]
    identity = appearance or description or (name or "道具")
    parts.append(f"道具定义：{identity}")
    parts.append(f"美术风格：{style}，全图统一。")
    parts.append(
        "画面要求：画面中只出现这一个指定道具（或明确规定为整体的道具组合）；"
        "主体完整、居中、清晰；明确物品的形状、颜色、材质、尺寸比例和辨识特征。"
    )
    parts.append(
        "必须保持：道具外观（形状、颜色、材质、结构与尺寸比例）与道具定义完全一致；"
        "背景为干净、统一的纯色或简单背景。"
    )
    parts.append(
        "禁止：不出现多个或重复物品；不出现角色、手部、人物剪影或无关物品；"
        "不出现文字、水印、边框、拼贴或装饰。"
    )
    parts.append("输出：单张完整道具锚定图，主体清晰独立。")
    return "\n".join(parts)


def negative_for_asset_type(asset_type: str) -> str:
    """按资产类型返回负面约束文本（供工作流声明 negative_prompt 槽位时注入）。"""
    return {
        "character": CHARACTER_NEGATIVE,
        "scene": SCENE_NEGATIVE,
        "prop": PROP_NEGATIVE,
    }.get(asset_type, "")
