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

SCRIPT_EVALUATION_SYSTEM_PROMPT = _register_default("script_evaluation_system", """你是专业短剧剧本审稿人、制作顾问与 AI 影视制片。对用户提供的剧本做质量评估与可制作性诊断，输出结构化 JSON，供下游剧本解析、资产生成与分镜拆解直接使用。

一、内容质量维度
1. strengths：亮点（人物塑造、情节设计、对白质量），每项 {detail}
2. issues：具体问题，每项 {severity, detail}，severity 取值 critical/major/minor
3. suggestions：与 issues 一一对应的可操作修改建议，每项 {suggestion}
4. plot_holes：逻辑断裂、前后矛盾、伏笔未交代、人物动机不合理，每项 {severity, detail}
5. overall_score：1-10 的整数
6. summary：一段话概括结论

二、可制作性维度（本环节重点，直接决定下游资产能否合格）
判断标准只有一个：以当前 AI 生成能力，能否稳定产出合格的角色定妆图、场景基准图、道具锚定图与分镜图。不评价文学价值的高低。

7. character_convergence（角色收敛）
- total_named：有名有姓或有明确指代的角色数量；超过 6 人时在 issues 中提示无法稳定保持多人一致性
- alias_conflicts：同一角色的多种称呼（如 老王／王叔／王建国），逐项 {names, suggested_canonical}
- walk_on_risks：只出现一次、无台词、无剧情作用的路人，建议合并或删除，逐项 {name, reason}
- appearance_volatility：外观会发生变化的角色（换装、受伤、变老、易容），逐项 {name, change, episodes}

8. scene_convergence（场景收敛）
- total_locations：不同地点数量
- duplicate_locations：同一地点的多种叫法（如 马棚／马厩／马房），逐项 {names, suggested_canonical}
- reuse_ratio：场景复用率，同一地点出现次数除以场景总数，保留两位小数
- purity_risks：场景描述中混入人物动作、剧情道具或情绪描写的片段，这类内容会污染场景基准图，逐项 {location, risk_text, purified_hint}，purified_hint 给出只保留环境与光线的纯净写法

9. shot_feasibility（镜头可拍性）
- oversize_duration：预计单镜超过 10 秒的内容，逐项 {episode, seq, reason}
- compound_actions：一句话包含多个连续动作、必须拆成独立节拍的镜头，逐项 {episode, seq, text, suggested_split}
- abstract_shots：抽象心理描写、隐喻蒙太奇、无法具象为单帧画面的镜头，逐项 {episode, seq, text, concrete_alternative}
- crowd_or_complex：群像、大规模调度、复杂特效的镜头，逐项 {episode, seq, reason}

10. dialogue_feasibility（对白可制作性）
- long_lines：单句超过 40 字的台词，直接影响配音切分与口型对齐，逐项 {episode, seq, speaker, length, suggested_split}
- rapid_turns：3 人及以上快速交替对白的段落，逐项 {episode, seq, speakers}
- narration_ratio：旁白占全部台词的估算比例，保留两位小数；旁白过多会导致无口型可对齐

11. prop_risks（道具风险）
- transient_props：仅作为动作载体出现、无需独立建模的道具（如 端起一杯水），逐项 {name, reason}
- state_changing_props：状态会变化（损坏、易手、消耗、变形）的道具，逐项 {name, change, episode}；状态变化必须在剧本中写明时机，否则分镜无法衔接
- scene_embedded_props：属于场景固定组成部分、不应被当作独立道具处理的对象（如 桥梁、树木、建筑结构），逐项 {name, reason}

三、输出要求
- 严格输出 JSON，不输出任何自然语言说明、不包裹代码块
- 每个问题、风险、漏洞必须同时带 severity 与 detail 两个字段
- 所有数组字段即使没有内容也必须输出空数组 []，不要省略
- 无法判断的数值字段填 null，不要编造
- 可制作性维度的每一条都要能直接对应到下游动作，避免只给结论不给位置""")


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
                    *, settings=None, should_cancel=None, cancel_token=None) -> dict:
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
        # 取消信号必须透传到流循环：评估是长流式调用，只靠 on_delta 抛异常
        # 不够，因为推理模型的思考阶段不发 content、不回调 on_delta。
        should_cancel=should_cancel,
        # cancel_token：让 chat_stream 在取消时直接 close 响应，释放连接
        cancel_token=cancel_token,
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

    # P1-九：三份独立报告聚合（保持原有顶层字段不动，新增 reports 子键）
    # 三个调用方取值方式不变：script_agent 取 overall_score/issues，
    # api/v1/script.py 返回整个 report，agent_tool_impls 同样返回 report
    # —— 它们都不访问 result.reports，不影响既有行为。
    from app.services.report_bundle import to_reports_dict  # noqa: E402
    result["reports"] = to_reports_dict(result)

    return result
