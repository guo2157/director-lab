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
import re

from app.services.prompt_store import _register_default
from app.services.script_chunker import SECTION_LINE_RE
from app.services.dialogue_audit import extract_dialogues, audit_dialogues

# 「人物名：台词」行，说话人限 1-10 字，避免把「第1集」误判为人物
_DIALOGUE_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_·]{0,9})\s*[:：]\s*(.+?)\s*$", re.M)
# 场次标题行（第X集 / 场次N），用于排除非人物行。
# 判定口径已抽到 script_chunker.SECTION_LINE_RE（分集切分与台词过滤共用一份），
# 此处保留 _SECTION_RE 别名，既有调用方与测试引用不变、行为不变。
_SECTION_RE = SECTION_LINE_RE
# 比对时需忽略的字符：全部标点与空白。只做等价剔除，不改动任何文字内容。
# 注意必须是剔除而不是全半角转换——『！』本身就是全角，转换不会让它消失，
# 会导致有标点与无标点的同一句台词得到两个不同指纹，去重失效。
_COMPARE_DROP_RE = re.compile(r"[\s，。？！：；、,。?!:;\"'“”‘’「」『』（）()《》〈〉\[\]【】\-—_…·~`|/\\*#@&%\$\^\+=<>]+")


def _strip_punct(text: str) -> str:
    """归一化用于宽松比对：去全部空白、统一全半角标点、剥离引号。

    只做等价变换，不改变语义词序，因此「张三：你好」与「张三： 你好！」
    在归一后都能互相包含，避免格式微调导致守恒校验误判。
    """
    return _COMPARE_DROP_RE.sub("", text)


def _regex_dialogues(source_text: str) -> list:
    """正则从原文抽取台词正文（不含说话人），作为守恒校验的可信基准。"""
    out = []
    for m in _DIALOGUE_RE.finditer(source_text or ""):
        line = m.group(2).strip()
        if not line:
            continue
        # 场次标题与「第X集」不是台词
        if _SECTION_RE.match(m.group(0)):
            continue
        if line not in out:
            out.append(line)
    return out


def _regex_characters(source_text: str) -> list:
    """正则从原文抽取说话人（即登场人物），作为人物守恒的可信基准。"""
    out = []
    for m in _DIALOGUE_RE.finditer(source_text or ""):
        name = m.group(1).strip()
        if not name or _SECTION_RE.match(name):
            continue
        if name not in out:
            out.append(name)
    return out

# ── 共享剧本格式规范（P3-1 方案 1：从 create/expand 的 4 条通用规范抽取）────
_SCRIPT_COMMON_FORMAT = "输出为标准剧本格式：场次标题（时间/地点）、场景描述、人物动作、对白（说话人+台词）、必要的旁白与声音提示。"
_SCRIPT_COMMON_EPISODE = "每集以「第X集」明确分隔；"
_SCRIPT_COMMON_CONSTRAINTS = "遵守用户给定的题材、受众、画风、集数、每集时长、语言与对白风格、内容尺度"
_SCRIPT_COMMON_OUTPUT = "只输出剧本正文，不输出"

SCRIPT_CREATE_SYSTEM_PROMPT = _register_default("script_create_system", (
    "你是专业短剧编剧。根据用户提供的创意或故事设定，创作一部完整的短剧剧本。\n"
    "创作规则：\n"
    "1. " + _SCRIPT_COMMON_FORMAT + "\n"
    "2. " + _SCRIPT_COMMON_EPISODE + "集内场次连续编号；对白逐条标注说话人。\n"
    "3. 人物名称全剧保持一致且稳定，不使用代称；同一地点使用统一场景名。\n"
    "4. " + _SCRIPT_COMMON_CONSTRAINTS + "、角色设定、世界观与创作要求。\n"
    "5. 每集保住开场吸引点、冲突升级、情绪高潮、结尾承接四节拍；对白口语化、符合人物性格。\n"
    "6. " + _SCRIPT_COMMON_OUTPUT + "创作说明或分析。\n"
))

SCRIPT_EXPAND_SYSTEM_PROMPT = _register_default("script_expand_system", (
    "你是专业短剧编剧。将用户提供的故事梗概扩写为完整的短剧剧本。\n"
    "扩写规则：\n"
    "1. 忠实保留梗概中的核心事件、人物关系与结局走向，不擅自改变主线。\n"
    "2. 在保留主线的前提下补全场次、动作细节、对白与情绪层次，使故事可拍摄。\n"
    "3. " + _SCRIPT_COMMON_FORMAT + "\n"
    "4. " + _SCRIPT_COMMON_EPISODE + "人物名称与场景名全剧一致稳定。\n"
    "5. " + _SCRIPT_COMMON_CONSTRAINTS + "与创作要求。\n"
    "6. " + _SCRIPT_COMMON_OUTPUT + "扩写说明。\n"
))

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


SCRIPT_NORMALIZE_SYSTEM_PROMPT = _register_default("script_normalize_system", """你是资深短剧编剧与剧本格式规范专家。用户会提供一份原始剧本素材，可能只是一个故事标题、一句话、一段梗概，也可能是格式混乱的完整剧本。你的任务是在不偏离用户原意的前提下，把素材整理成符合影视短剧标准格式的可拍摄剧本。

三层处理策略，按输入信息量选择，宁可少补不可乱补：
第一层 格式规范化（任何输入都执行，不产生新剧情）：每集以「第X集」明确分隔；集内场次连续编号；场次标题包含时间与地点，格式为「场次N 时间·地点」；场景描述、人物动作、对白分层书写；对白格式为「人物名：台词」；旁白与声音提示单独成行；人物名称全剧统一，不使用他、那个人等代称。
第二层 语义明确化（原文存在含混表述时执行）：把模糊指代落实为具体描述，例如把他走进来补全为张三推门走进屋内；补全信息必须可从原文上下文推导，不得引入原文没有的事件、人物、地点与结局。
第三层 结构性补全（输入信息量不足一集时执行，必须标注为 AI 补全）：若输入只有一个标题或一句话，不足以支撑一集剧本，此时禁止自行编造完整剧情，必须在 questions 字段提出最少必要的追问，包括主角是谁、核心冲突是什么、结局走向、目标集数与单集时长；若用户已在创作配置中给出集数与单集时长，可在该框架内补全场次与对白，但所有补全内容必须在 changes 中标明 ai_generated 为 true。

四条硬约束，违反即视为失败：
1. 人物集合守恒：normalized_characters 必须包含 original_characters 中全部人物，不得新增原文未出现的主要角色。
2. 原台词保真：original_dialogues 中每条台词必须原样出现在 normalized_text 中，只允许修正错别字，不得改写语义、不得替换说话人。
3. 场景与道具不得凭空增加，确需新增必须在 changes 中写明依据。
4. 不得改动结局走向与人物关系。

输出严格 JSON，字段说明：
- normalized_text：规范化后的完整剧本正文，字符串
- level：本次实际执行到第几层，整数 1 或 2 或 3
- original_characters：原文出现的人物名数组，原样摘录
- normalized_characters：优化稿出现的全部人物名数组
- original_dialogues：原文已有的台词原文数组，逐条摘录，没有则为空数组
- changes：变更清单数组，每项含 category（format 或 clarity 或 completion）、summary（一句话说明改了什么）、evidence（对应原文哪一句作为依据，无法对应时为空字符串）、ai_generated（布尔，是否属于模型补出的内容）
- questions：需要用户补充的关键要素数组，信息充足时为空数组

只输出 JSON，不要输出任何解释文字。""")


def _config_lines(config: dict) -> list:
    """把项目创作配置转成提示词可读行（与创作/扩写共用同一套键）。"""
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
        "worldview": "故事世界观",
        "requirements": "创作要求",
    }
    lines = []
    for key, label in label_map.items():
        val = (config or {}).get(key)
        if val not in (None, ""):
            lines.append(f"{label}：{val}")
    return lines


def build_normalize_user_prompt(source_text: str, config: dict) -> str:
    """组装规范化校验的用户提示词：源文本 + 创作配置（供模型约束补全范围）。"""
    parts = ["请对以下剧本素材做规范化校验与必要补全：", source_text]
    lines = _config_lines(config)
    if lines:
        parts.append("创作配置：")
        parts.extend(lines)
    return "\n".join(parts)


def verify_normalize_output(raw: dict, source_text: str) -> dict:
    """机器校验规范化结果：人物集合守恒 + 原台词保真。

    2026-11-07 修复「采用优化稿」按钮恒灰：
    原实现把基准完全交给模型自报的 original_characters / original_dialogues，
    再用裸子串匹配判定，导致两类必然误判：
      ① 模型摘录台词时补了说话人或改了标点，d not in text 直接成立；
      ② 模型少报时基准本身不完整，校验形同虚设。
    现改为正则从原文抽取为主基准（不依赖模型）、模型自报为补充，
    比对前双方都做标点与空白归一，仅格式微调降级为提示，不再阻断采用。
    """
    text = str(raw.get("normalized_text") or "").strip()
    if not text:
        raise ValueError('规范化结果为空')
    # 基准一：正则从原文实际抽取（可信来源）
    regex_chars = _regex_characters(source_text)
    regex_dialogues = _regex_dialogues(source_text)
    # 基准二：模型自报（补充，按出现顺序去重合并）
    llm_chars = [str(x).strip() for x in (raw.get("original_characters") or []) if str(x).strip()]
    llm_dialogues = [str(x).strip() for x in (raw.get("original_dialogues") or []) if str(x).strip()]
    orig_chars = list(dict.fromkeys(regex_chars + llm_chars))
    orig_dialogues = list(dict.fromkeys(regex_dialogues + llm_dialogues))
    norm_chars = [str(x).strip() for x in (raw.get("normalized_characters") or []) if str(x).strip()]
    norm_chars_norm = {_strip_punct(c) for c in norm_chars if _strip_punct(c)}
    # 优化稿正文同样参与人物判定：模型漏填 normalized_characters 时不误判为丢失
    text_norm = _strip_punct(text)
    missing_chars = []
    for c in orig_chars:
        key = _strip_punct(c)
        if not key:
            continue
        if key in norm_chars_norm or key in text_norm:
            continue
        missing_chars.append(c)
    missing_dialogues = []
    punct_warnings = []
    # 按归一化指纹去重：正则与模型两个来源可能给出同一句的不同写法（有无标点、
    # 有无说话人前缀），不去重会把同一句台词报成两条缺失
    seen_missing = set()
    seen_warn = set()
    for d in orig_dialogues:
        # 模型自报条目可能带「人物：」前缀，比对时剥离后再匹配
        body = re.sub(r"^[^：:]{1,10}[：:]", "", d).strip()
        key = _strip_punct(body) or _strip_punct(d)
        if not key:
            continue
        if key in text_norm or _strip_punct(d) in text_norm:
            # 归一后才匹配上，说明存在标点/空白差异：提示但不阻断
            if d not in text and body not in text and key not in seen_warn:
                seen_warn.add(key)
                punct_warnings.append(d[:40])
            continue
        if key in seen_missing:
            continue
        seen_missing.add(key)
        missing_dialogues.append(d)
    changes = []
    for c in (raw.get("changes") or []):
        if not isinstance(c, dict):
            continue
        changes.append({
            "category": str(c.get("category") or "format"),
            "summary": str(c.get("summary") or ""),
            "evidence": str(c.get("evidence") or ""),
            "ai_generated": bool(c.get("ai_generated")),
        })
    questions = [str(q).strip() for q in (raw.get("questions") or []) if str(q).strip()]
    try:
        level = int(raw.get("level") or 1)
    except (TypeError, ValueError):
        level = 1
    # 对白对账：原文→规范化结果 逐条匹配（preserved/modified/missing/…）
    src_dialogues = extract_dialogues(source_text)
    dialogue_audit = audit_dialogues(src_dialogues, text)
    # blocked 判定：人物丢失 或 对白有实质性缺失（missing/modified = 实质性变化）
    dialogue_blocked = dialogue_audit.blocked
    return {
        "normalized_text": text,
        "level": level,
        "changes": changes,
        "questions": questions,
        "checks": {
            "characters_preserved": not missing_chars,
            "dialogue_preserved": not missing_dialogues,
            "missing_characters": missing_chars,
            "missing_dialogues": missing_dialogues,
            # 仅标点/空白差异的台词：仅提示，不阻断采用
            "punct_warnings": punct_warnings,
            "blocked": bool(missing_chars or missing_dialogues or dialogue_blocked),
            # 基准来源计数，便于排查「模型漏报」与「正则漏抽」
            "baseline": {
                "regex_characters": len(regex_chars),
                "regex_dialogues": len(regex_dialogues),
                "llm_dialogues": len(llm_dialogues),
            },
        },
        # 对白保真审计报告（新增）
        "dialogue_audit": dialogue_audit.to_dict(),
        "original_chars": len(source_text),
        "normalized_chars": len(text),
    }


def normalize_script(source_text: str, config: dict) -> dict:
    """剧本规范化校验：保真前提下整理为标准剧本格式，返回优化稿与变更清单。

    LLM 错误向上抛 LLMError，由调用方转为 HTTP 错误；本函数不落库。
    """
    from app.integrations.llm import LLMClient, LLMError
    from app.services.prompt_store import get_prompt

    system = get_prompt("script_normalize_system")
    user = build_normalize_user_prompt(source_text, config or {})
    client = LLMClient()
    raw = client.chat_json(system, user)
    try:
        return verify_normalize_output(raw, source_text)
    except ValueError as e:
        raise LLMError('llm_invalid_output', str(e))


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
