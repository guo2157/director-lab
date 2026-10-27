"""剧本创作服务（阶段1）：AI 创作与扩写剧本入口。

设计约定：
- 两种模式：create（创意→剧本）/ expand（梗概→扩写剧本）；已有剧本直接走 script-versions 保存，不经本服务
- 创作配置存 Project.settings（genre/audience/visual_style/episode_count/episode_duration_target/
  language/dialogue_style/content_rating/character_settings/worldview/requirements），
  修改配置后由既有 estimated_text_hash 机制驱动下游失效，不无条件清空资产
- 提示词经 prompt_store 外置（script_create_system / script_expand_system），UI 可编辑即时生效
- LLM 调用为同步短事务外调用（与 estimate 同模式）；失败按 llm_* 错误码分类，不落库半成品
- 生成结果作为新剧本版本保存（ScriptVersion 版本号+1），原文与生成稿均保留可追溯
"""
from app.services.prompt_store import _register_default

SCRIPT_CREATE_SYSTEM_PROMPT = _register_default("script_create_system", """你是专业短剧编剧。根据用户提供的创意或故事设定，创作一部完整的短剧剧本。
创作规则：
1. 输出为标准剧本格式：场次标题（时间/地点）、场景描述、人物动作、对白（说话人+台词）、必要的旁白与声音提示。
2. 每集以「第X集」明确分隔；集内场次连续编号；对白逐条标注说话人。
3. 人物名称全剧保持一致且稳定，不使用代称；同一地点使用统一场景名。
4. 遵守用户给定的题材、受众、画风、集数、每集时长、语言与对白风格、内容尺度、角色设定、世界观与创作要求。
5. 每集保住开场吸引点、冲突升级、情绪高潮、结尾承接四节拍；对白口语化、符合人物性格。
6. 只输出剧本正文，不输出创作说明或分析。
""")

SCRIPT_EXPAND_SYSTEM_PROMPT = _register_default("script_expand_system", """你是专业短剧编剧。将用户提供的故事梗概扩写为完整的短剧剧本。
扩写规则：
1. 忠实保留梗概中的核心事件、人物关系与结局走向，不擅自改变主线。
2. 在保留主线的前提下补全场次、动作细节、对白与情绪层次，使故事可拍摄。
3. 输出为标准剧本格式：场次标题（时间/地点）、场景描述、人物动作、对白（说话人+台词）、必要的旁白与声音提示。
4. 每集以「第X集」明确分隔；人物名称与场景名全剧一致稳定。
5. 遵守用户给定的题材、受众、画风、集数、每集时长、语言与对白风格、内容尺度与创作要求。
6. 只输出剧本正文，不输出扩写说明。
""")

# 创作配置键（存 Project.settings，与评估/解析共用命名空间）
WRITING_CONFIG_KEYS = [
    "genre",                 # 题材
    "audience",              # 目标受众
    "visual_style",          # 真人/动画/其他视觉风格
    "episode_count",         # 集数
    "episode_duration_target",  # 每集目标时长（秒）
    "language",              # 语言
    "dialogue_style",        # 对白风格
    "content_rating",        # 内容尺度
    "character_settings",    # 用户指定的角色设定
    "worldview",             # 故事世界观
    "requirements",          # 其他创作要求
]


def build_writing_user_prompt(mode: str, source_text: str, config: dict) -> str:
    """组装创作/扩写的用户提示词：源文本 + 创作配置分段。"""
    if mode not in ("create", "expand"):
        raise ValueError(f"非法创作模式: {mode}")
    parts = []
    if mode == "create":
        parts.append("请根据以下创意创作短剧剧本：")
    else:
        parts.append("请将以下故事梗概扩写为短剧剧本：")
    parts.append(source_text)
    cfg_lines = []
    label_map = {
        "genre": "题材",
        "audience": "目标受众",
        "visual_style": "视觉风格",
        "episode_count": "集数",
        "episode_duration_target": "每集目标时长（秒）",
        "language": "语言",
        "dialogue_style": "对白风格",
        "content_rating": "内容尺度",
        "character_settings": "角色设定",
        "worldview": "世界观",
        "requirements": "创作要求",
    }
    for key, label in label_map.items():
        val = (config or {}).get(key)
        if val not in (None, ""):
            cfg_lines.append(f"{label}：{val}")
    if cfg_lines:
        parts.append("创作配置：")
        parts.extend(cfg_lines)
    return "\n".join(parts)


def write_script(mode: str, source_text: str, config: dict) -> str:
    """调用语言模型创作/扩写剧本，返回剧本正文。

    LLM 错误向上抛 LLMError（llm_unconfigured/llm_unreachable/llm_timeout/llm_error），
    由调用方转为 HTTP 错误，不落库半成品。
    """
    from app.integrations.llm import LLMClient
    from app.services.prompt_store import get_prompt

    system = get_prompt("script_create_system" if mode == "create" else "script_expand_system")
    user = build_writing_user_prompt(mode, source_text, config or {})
    client = LLMClient()
    return client.chat(system, user, json_mode=False).strip()
