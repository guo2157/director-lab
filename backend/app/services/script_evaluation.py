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


def evaluate_script(script_text: str, estimate_data: dict, config: dict, on_delta=None) -> dict:
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
    from app.integrations.llm import LLMClient, parse_llm_json
    from app.services.prompt_store import get_prompt

    system = get_prompt("script_evaluation_system")
    user = build_evaluation_user_prompt(script_text, estimate_data or {}, config or {})
    client = LLMClient()
    if on_delta is not None:
        content = client.chat_stream(system, user, json_mode=True, on_delta=on_delta)
        result = parse_llm_json(content)
    else:
        result = client.chat_json(system, user)

    # 校验返回结构基本字段
    required_keys = {"strengths", "issues", "suggestions", "production_risks",
                     "plot_holes", "overall_score", "summary"}
    missing = required_keys - set(result.keys())
    if missing:
        raise ValueError(f"评估报告缺少必需字段: {missing}")

    # 校验 overall_score 范围
    score = result.get("overall_score")
    if not isinstance(score, (int, float)) or score < 1 or score > 10:
        result["overall_score"] = max(1, min(10, int(score) if score else 5))

    return result
