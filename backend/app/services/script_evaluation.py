"""剧本质量评估服务（阶段2）：AI 生成结构化评估报告。

设计约定：
- 在 analyze_script_outline LLM 大纲分析基础上，调用 LLM 对剧本进行深度质量评估
- 评估维度：优点、问题、修改建议、制作风险、剧情漏洞检测
- 提示词经 prompt_store 外置（script_evaluation_system），UI 可编辑即时生效
- LLM 调用为同步短事务外调用；失败按 llm_* 错误码分类，不落库半成品
- 评估结果以 JSON 结构返回，由 API 层决定是否持久化
"""
import json
from app.services.prompt_store import _register_default

SCRIPT_EVALUATION_SYSTEM_PROMPT = _register_default("script_evaluation_system", """你是专业短剧剧本审稿人和制作顾问。对用户提供的剧本进行全面质量评估。

评估维度：
1. strengths（优点）：剧本的亮点和优势，如人物塑造、情节设计、对白质量等
2. issues（问题）：存在的具体问题，如逻辑不通、节奏拖沓、人物扁平、对白不自然等
3. suggestions（修改建议）：针对每个问题给出具体可操作的修改建议
4. production_risks（制作风险）：拍摄/制作层面可能遇到的困难，如场景复杂度、特效需求、演员调度等
5. plot_holes（剧情漏洞）：逻辑断裂、前后矛盾、未交代的伏笔、人物动机不合理等
6. overall_score（总体评分）：1-10 分，综合评估剧本质量
7. summary（总结）：一段话概括评估结论

输出要求：
- 严格输出 JSON，不要输出其他内容
- 每个问题/风险/漏洞需包含 severity（critical/major/minor）和 detail 字段
- 修改建议需与问题一一对应，包含 suggestion 字段
- 评估应结合大纲分析数据（字数、预估集数、每集时长）进行可制作性分析
""")


# 可制作性维度（第 1 步新增）：按 AI 生成能力评估剧本能否稳定产出合格资产。
# 缺失时补空结构，保证下游可安全索引而不抛错。
_PRODUCIBILITY_DEFAULTS: dict[str, dict] = {
    "character_convergence": {
        "total_named": None, "alias_conflicts": [], "walk_on_risks": [],
        "appearance_volatility": [],
    },
    "scene_convergence": {
        "total_locations": None, "duplicate_locations": [], "reuse_ratio": None,
        "purity_risks": [],
    },
    "shot_feasibility": {
        "oversize_duration": [], "compound_actions": [], "abstract_shots": [],
        "crowd_or_complex": [],
    },
    "dialogue_feasibility": {
        "long_lines": [], "rapid_turns": [], "narration_ratio": None,
    },
    "prop_risks": {
        "transient_props": [], "state_changing_props": [], "scene_embedded_props": [],
    },
}


def _normalize_producibility(result: dict) -> list[str]:
    """把可制作性维度补齐为固定结构，返回实际缺失的维度名。

    LLM 未按新模板输出时不让整次评估失败：补空结构后下游仍可安全读取，
    但缺失项要返回给调用方留痕，避免出现『看起来评估过』的假象。
    """
    missing: list[str] = []
    for key, defaults in _PRODUCIBILITY_DEFAULTS.items():
        block = result.get(key)
        if not isinstance(block, dict):
            result[key] = {k: (list(v) if isinstance(v, list) else v) for k, v in defaults.items()}
            missing.append(key)
            continue
        for field, default in defaults.items():
            if field not in block:
                block[field] = list(default) if isinstance(default, list) else default
    return missing


def build_evaluation_user_prompt(script_text: str, estimate_data: dict, config: dict) -> str:
    """组装评估用户提示词：剧本正文 + 算法估算数据 + 创作配置。"""
    parts = ["请对以下剧本进行全面质量评估："]
    parts.append(script_text)

    # 注入 LLM 大纲分析数据
    parts.append("\n--- 大纲分析数据 ---")
    parts.append(f"字数：{estimate_data.get('char_count', '未知')}")
    parts.append(f"对白字数估算：{estimate_data.get('dialogue_char_estimate', '未知')}")
    parts.append(f"预估总时长：{estimate_data.get('estimated_total_duration', '未知'):.1f} 秒")
    parts.append(f"预估集数：{estimate_data.get('estimated_episodes', '未知')}")
    parts.append(f"算法版本：{estimate_data.get('algorithm_version', '未知')}")

    breakdown = estimate_data.get("episode_breakdown", [])
    if breakdown:
        parts.append("分集估算：")
        for ep in breakdown:
            parts.append(
                f"  第{ep['episode']}集：预估时长 {ep['estimated_duration']:.1f}s，"
                f"预估镜头数 {ep['estimated_shots']}"
            )

    # 注入创作配置
    cfg_lines = []
    label_map = {
        "genre": "题材",
        "audience": "目标受众",
        "visual_style": "视觉风格",
        "episode_count": "目标集数",
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
        parts.append("\n--- 创作配置 ---")
        parts.extend(cfg_lines)

    return "\n".join(parts)


def _sample_episodes_for_evaluation(script_text: str) -> tuple[str, list[int]]:
    """超预算时抽样：人物表 + 第 1–2 集 + 中间等距 2 集 + 最后 1 集。

    返回 ``(抽样正文, 被抽中的集号列表)``。无集标记时原样返回全文（评估本身是
    软闸口，失败也不阻断任务，这里不做激进裁剪）。

    抽样而非截断的原因：评估要看剧本全貌（开局钩子、中段节奏、收尾），
    单纯截断前 N 字会把后段结构性问题全部漏掉。
    """
    from app.services.script_chunker import split_episodes

    split = split_episodes(script_text)
    if split is None or not split.chunks:
        return script_text, []

    chunks = split.chunks
    total = len(chunks)
    picked: list = []
    if total <= 6:
        picked = list(chunks)
    else:
        indexes = {0, 1, total - 1}
        # 中间等距取 2 集
        mid = [i for i in range(2, total - 1)]
        if mid:
            step = max(1, len(mid) // 2)
            for i in mid[::step][:2]:
                indexes.add(i)
        picked = [chunks[i] for i in sorted(indexes)]

    # 这里必须字符串拼接：对单个字符串用 "\n".join(...) 会把 preamble 逐字符拆开
    prefix = ""
    if split.preamble.strip():
        prefix = "[人物表/序言]\n" + split.preamble.strip() + "\n\n"
    body = prefix + "\n".join(c.text for c in picked)
    return body, [c.number for c in picked]


def evaluate_script(script_text: str, estimate_data: dict, config: dict, on_delta=None,
                    *, settings=None) -> dict:
    """调用语言模型评估剧本质量，返回结构化评估报告。
    on_delta 不为 None 时使用流式调用，增量文本经回调实时推送（供前端展示评估过程）。

    返回结构：
    {
        "strengths": [{"detail": str}],
        "issues": [{"severity": "critical|major|minor", "detail": str}],
        "suggestions": [{"suggestion": str}],
        "production_risks": [{"severity": "critical|major|minor", "detail": str}],
        "plot_holes": [{"severity": "critical|major|minor", "detail": str}],
        "overall_score": int,
        "summary": str
    }

    LLM 错误向上抛 LLMError（llm_unconfigured/llm_unreachable/llm_timeout/llm_error/llm_invalid_output），
    由调用方转为 HTTP 错误，不落库半成品。
    """
    from app.integrations.llm import LLMClient
    from app.integrations.llm_response import parse_json_safely
    from app.integrations.llm_retry import call_with_retry
    from app.services.prompt_store import get_prompt

    from app.integrations.token_estimate import estimate_input_tokens, input_budget

    system = get_prompt("script_evaluation_system")
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()

    # 超预算时改为抽样评估：评估是软闸口，抽样足以发现结构性问题，
    # 而整本超上下文会让供应商直接报错（等于什么也没评估）。
    budget = input_budget(settings)
    eval_text = script_text
    sampled_episodes: list[int] = []
    scope = "full"
    if budget > 0 and estimate_input_tokens(script_text, settings=settings) > budget:
        eval_text, sampled_episodes = _sample_episodes_for_evaluation(script_text)
        scope = "sampled"

    user = build_evaluation_user_prompt(eval_text, estimate_data or {}, config or {})
    client = LLMClient()
    # 统一重试：空正文/截断/非 JSON 按错误码分流，JSON 解析在循环内
    result = call_with_retry(
        client,
        system_prompt=system,
        user_prompt=user,
        json_mode=True,
        stream=on_delta is not None,
        on_delta=on_delta,
        use_chat_json=on_delta is None,
    )

    # 流式路径（on_delta 非 None）下 call_with_retry 的 use_chat_json=False，
    # 返回的是**原始文本**而不是 dict。旧实现直接对返回值做 .keys() 校验，
    # 必然抛 "'str' object has no attribute 'keys'"，被 agent 的软闸口静默跳过，
    # 导致 Agent 主路径的评估事实上从未产出过。这里显式解析。
    if isinstance(result, str):
        # 注意：parse_json_safely 返回 (对象, ValidationResult) 元组
        parsed, check = parse_json_safely(result, expect=dict)
        if not isinstance(parsed, dict):
            reason = getattr(check, "message", None) or getattr(check, "error_code", "unknown")
            raise ValueError(f"评估返回无法解析为 JSON 对象: {reason}")
        result = parsed

    # 校验返回结构基本字段
    required_keys = {"strengths", "issues", "suggestions", "production_risks",
                     "plot_holes", "overall_score", "summary"}
    missing = required_keys - set(result.keys())
    if missing:
        raise ValueError(f"评估报告缺少必需字段: {missing}")

    # 评估范围标记：sampled 表示因超预算只评估了抽样集（前端可据此提示）
    result["evaluation_scope"] = scope
    result["sampled_episodes"] = sampled_episodes

    # 可制作性维度：缺失不阻断（软闸口语义），补空结构并留痕，避免下游索引异常
    result["producibility_missing"] = _normalize_producibility(result)

    # 校验 overall_score 范围
    score = result.get("overall_score")
    if not isinstance(score, (int, float)) or score < 1 or score > 10:
        result["overall_score"] = max(1, min(10, int(score) if score else 5))

    return result
