"""资产纯净化提示词模板（P1）：角色定妆图/场景基准图/道具锚定图的统一结构化生成模板。

设计约定（对应资产纯净化与分镜一致性专项改造）：
- 静态提示词外置为 workspace/prompts/asset_*.md（可 UI 编辑），动态变量运行时注入
- Schema / 权限规则 / 必填校验 / 负面约束 / 默认风格保留在代码内，不进入外置模板
- 资产图用于定义固定身份，不讲述剧情：单一主体、干净背景、无文字水印、无拼贴
- 外置文件缺失时回退到模块内的 DEFAULT_PROMPTS 兜底模板（零配置可用）
"""

from app.services.prompt_store import _register_default, get_prompt

# 统一美术风格占位：项目未配置统一风格时使用中性描述，不虚构具体风格
_DEFAULT_STYLE = "童话绘本写实风格，柔和饱和色彩，温暖光照，细腻质感"
# 模板版本（P4 整改）：变更时同步更新，由调用方写入 input_snapshot 供追溯
ASSET_PROMPTS_VERSION = "2.1"

# ── 外置模板兜底（静态提示词 + {name}/{identity}/{style} 占位）──────────

_register_default("asset_character", """任务：角色定妆图（角色资产图），定义角色『{name}』的固定视觉身份，不讲述剧情。

角色定义：{identity}

美术风格：{style}，全图统一。

画面要求：画面中只出现这一个指定角色；全身正面或轻微四分之三视角；站立自然，姿势中性；双手、双腿和身体结构完整清晰；主体完整居中，不被裁切。

必须保持：角色身份特征（物种、年龄感、体型、毛色或肤色、脸型、发型或鬃毛、耳朵、尾巴、服装与配饰）与角色定义完全一致；背景为干净、统一、低干扰的纯色或简单摄影棚背景。

禁止：不出现第二个人物、人物剪影、镜中人物或背景人物；不与其他角色互动；不手持剧情道具（除非该物品是角色固定外观的一部分）；不出现标题、标签、文字、水印、边框、拼贴或装饰。

输出：单张完整角色定妆图，主体清晰独立，细节完整。""")

_register_default("asset_scene", """任务：场景基准图（场景资产图），定义场景『{name}』的环境与空间关系，不出现角色。

场景定义：{identity}

美术风格：{style}，全图统一。

画面要求：只定义这一个场景；保持明确的空间布局、地面、背景、光照和色彩；环境组成部分（如地面、草地、树木、河水、河岸、建筑结构）按场景定义呈现；保持可用于后续镜头构图的空间深度。

场景纯净度：本任务只生成环境，不生成剧情内容。
- 画面中不出现任何人物、动物、角色剪影或人体局部。
- 不出现剧情中的可移动道具、手持物品、交通工具或临时摆放物品。
- 不出现角色、动物或道具的倒影、影子、海报形象、雕像形象等间接表现。
- 不擅自添加原始场景描述中没有的家具、物品、建筑或装饰。
- 允许保留明确属于环境本身的固定景观和固定布景，例如树木、岩石、河流、固定桥梁和建筑结构。
- 不出现文字、字幕、标签、Logo、水印、边框、分镜编号或拼图布局。

必须保持：环境结构与空间布局与场景定义一致；主色调与光照设定统一。

输出：单张完整场景基准图，空间结构清晰。""")

_register_default("asset_prop", """任务：道具锚定图（道具资产图），定义道具『{name}』的固定外观，不出现角色。

道具定义：{identity}

美术风格：{style}，全图统一。

画面要求：画面中只出现这一个指定道具（或明确规定为整体的道具组合）；主体完整、居中、清晰；明确物品的形状、颜色、材质、尺寸比例和辨识特征。

必须保持：道具外观（形状、颜色、材质、结构与尺寸比例）与道具定义完全一致；背景为干净、统一的纯色或简单背景。

禁止：不出现多个或重复物品；不出现角色、手部、人物剪影或无关物品；不出现文字、水印、边框、拼贴或装饰。

输出：单张完整道具锚定图，主体清晰独立。""")


def _require_identity(*values: str, asset_label: str) -> None:
    """必填变量校验：名称/外观/描述全部为空时明确报错，不静默生成占位提示词。"""
    if not any((v or "").strip() for v in values):
        raise ValueError(
            f"{asset_label}_prompt_missing_identity: "
            f"{asset_label}的名称、外观描述与简介均为空，无法构建资产图提示词"
        )

# 历史 P1 负向词已收编至 prompt_compiler._LEGACY_NEGATIVE（P1-2 单一事实源），此处不再重复定义。


def build_character_prompt(
    name: str,
    appearance: str,
    *,
    description: str = "",
    style: str = "",
) -> str:
    """构建角色定妆图纯净化提示词（P1 外置）。

    静态模板从 workspace/prompts/asset_character.md 读取（缺失回退 DEFAULT_PROMPTS），
    变量 {name}/{identity}/{style} 由代码运行时注入；必填校验、默认风格、负面约束保留在代码内。
    """
    _require_identity(name, appearance, description, asset_label="角色")
    template = get_prompt("asset_character")
    identity = appearance or description or (name or "角色")
    style = style or _DEFAULT_STYLE
    return template.format(name=name, identity=identity, style=style)


def build_scene_prompt(
    title: str,
    description: str,
    *,
    style: str = "",
) -> str:
    """构建场景基准图纯净化提示词（P1 外置）。

    静态模板从 workspace/prompts/asset_scene.md 读取（缺失回退 DEFAULT_PROMPTS），
    {name} 用 title、{identity} 用拼接、{style} 美术风格由代码注入。
    """
    _require_identity(title, description, asset_label="场景")
    template = get_prompt("asset_scene")
    name = title or "场景"
    identity = f"{title}。{description}" if title and description else (description or title or "场景")
    style = style or _DEFAULT_STYLE
    return template.format(name=name, identity=identity, style=style)


def build_prop_prompt(
    name: str,
    appearance: str,
    *,
    description: str = "",
    style: str = "",
) -> str:
    """构建道具锚定图纯净化提示词（P1 外置）。

    静态模板从 workspace/prompts/asset_prop.md 读取（缺失回退 DEFAULT_PROMPTS），
    变量 {name}/{identity}/{style} 由代码运行时注入。
    """
    _require_identity(name, appearance, description, asset_label="道具")
    template = get_prompt("asset_prop")
    identity = appearance or description or (name or "道具")
    style = style or _DEFAULT_STYLE
    return template.format(name=name, identity=identity, style=style)


def negative_for_asset_type(asset_type: str) -> str:
    """按资产类型返回负面约束文本（供工作流声明 negative_prompt 槽位时注入）。

    P1-2 起委托给 prompt_compiler.negative_for：通用层 + 类型层 + 历史层合并去重，
    与编译器链路输出一致；函数内惰性导入避免与 prompt_compiler 循环依赖。
    """
    from app.services.prompt_compiler import negative_for
    return negative_for(asset_type)
