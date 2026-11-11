"""剧本评估与解析服务：PROMPTS.md §2/§3 规范的实现。

- estimate：集数与时长分析。LLM 大纲分析（analyze_script_outline）输出分集结构，
  代码规则估算已移除，一律以 LLM 分析为准（API.md §5 estimate 约定）
- parse：调用语言模型一次性解析剧本，按归一化规则（P0 强约束）补齐字段
- 归一化：补齐 shot_kind/duration/time_jump/requires_prev_frame、
  speaker_id 按角色名映射（失败保留原名并记审核问题）、emotion 缺失落 neutral 记警告
所有外部调用（语言模型）在短数据库事务之外执行（AGENTS.md §3-4）。
"""
import re
from dataclasses import dataclass, field

# --- 大纲分析版本（LLM 大纲替代代码规则估算，集数一律以 LLM 分析为准） ---
OUTLINE_ALGORITHM_VERSION = "llm-outline-v1.0"
# 单镜平均时长（秒）：归一化补齐 duration 默认值
AVG_SHOT_DURATION = 10.0
# 单集目标时长（秒），来自项目 settings.episode_duration_target 默认值
DEFAULT_EPISODE_DURATION = 90
DEFAULT_SHOTS_PER_EPISODE = 9


# --- 归一化去重（两级归一化策略：精确名 → 归一化名） ---
def normalize_entity_name(name: str) -> str:
    """角色/道具归一化：去成对中/英文括号内容（定位/别名）→ 去空白 → 小写。"""
    s = (name or "").strip()
    s = re.sub(r'[（(][^（）()]*[)）]', "", s)
    s = re.sub(r'[（(].*$', "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.lower()


def normalize_scene_name(title: str, time_note) -> str:
    """场景归一化：地点+时段复合键（保留括号区分子场景）。"""
    t = re.sub(r"[\s\u3000]+", "", (title or "")).lower()
    tn = re.sub(r"[\s\u3000]+", "", (time_note or "")).lower()
    return t + "|" + tn if tn else t


# --- 剧本解析系统提示词（PROMPTS.md §2 模板浓缩，输出 §3 结构 JSON） ---
# D-15 提示词外置：此常量为内置默认值；运行时经 prompt_store.get_prompt 动态读取，
# workspace/prompts/parse_system.md 存在时优先生效（UI 编辑保存即生效，无需重启）。
from app.services.prompt_store import _register_default, get_prompt

# --- 剧本大纲分析系统提示词（LLM 大纲替代代码规则估算，集数一律以 LLM 分析为准） ---
OUTLINE_SYSTEM_PROMPT = _register_default("script_outline_system", """你是专业短剧结构分析师。分析用户提供的剧本，输出分集大纲，供下游拆批解析与制作排期使用。
输出要求：
1. 严格输出 JSON，不要输出其他内容。
2. 顶层字段：total_episodes（int，总集数）、estimated_total_duration（int，预估总时长秒）、dialogue_char_estimate（int，对白总字数预估）、episodes（数组）。
3. episodes 每项含 episode（集号，从 1 开始连续）、title（该集标题，一句话）、estimated_duration（该集预估时长秒）、estimated_shots（该集预估镜头数）。
4. 分集依据：剧本有明确分集标记（如『第X集』）时严格按标记划分；无标记时按剧情节奏与用户给出的目标单集时长划分。
5. estimated_shots 按每镜平均 10 秒估算；时长与镜头数为预估值。
只输出 JSON。
""")


def analyze_script_outline(text: str, *, episode_duration_target: int = DEFAULT_EPISODE_DURATION,
                           max_episodes_per_batch: int = 10, client=None) -> dict:
    """LLM 大纲分析：集数、每集时长与镜头数一律以 LLM 分析为准（代码规则估算已移除）。

    返回结构与原估算对象兼容（algorithm_version/char_count/dialogue_char_estimate/
    estimated_total_duration/estimated_episodes/episode_breakdown/exceeds_batch_limit/
    suggestion），供评估提示词与 estimate 端点复用。
    LLM 失败向上抛 LLMError，由调用方决定失败处理。
    """
    from app.integrations.llm import LLMClient, LLMError
    if client is None:
        client = LLMClient()
    user = (
        f"目标单集时长：{episode_duration_target} 秒。\n"
        f"请分析以下剧本的分集结构：\n\n{text}"
    )
    result = client.chat_json(get_prompt("script_outline_system"), user)
    try:
        total = int(result.get("total_episodes") or 0)
    except (TypeError, ValueError):
        total = 0
    if total < 1:
        raise LLMError("llm_invalid_output", "大纲分析未返回有效 total_episodes")
    breakdown = []
    for ep in result.get("episodes") or []:
        try:
            breakdown.append({
                "episode": int(ep.get("episode") or 0),
                "title": str(ep.get("title") or ""),
                "estimated_duration": round(float(ep.get("estimated_duration") or episode_duration_target), 1),
                "estimated_shots": max(1, int(ep.get("estimated_shots") or 1)),
            })
        except (TypeError, ValueError):
            continue
    if not breakdown:
        breakdown = [{
            "episode": i, "title": "",
            "estimated_duration": float(episode_duration_target),
            "estimated_shots": max(1, round(episode_duration_target / AVG_SHOT_DURATION)),
        } for i in range(1, total + 1)]
    total_duration = sum(e["estimated_duration"] for e in breakdown)
    exceeds = total > max_episodes_per_batch
    suggestion = (
        f"剧本共 {total} 集，超过单次制作上限 {max_episodes_per_batch} 集，建议分批制作"
        if exceeds else None
    )
    try:
        dialogue_chars = int(result.get("dialogue_char_estimate") or 0)
    except (TypeError, ValueError):
        dialogue_chars = 0
    return {
        "algorithm_version": OUTLINE_ALGORITHM_VERSION,
        "char_count": len(text),
        "dialogue_char_estimate": dialogue_chars,
        "estimated_total_duration": round(total_duration, 1),
        "estimated_episodes": total,
        "episode_breakdown": breakdown,
        "exceeds_batch_limit": exceeds,
        "suggestion": suggestion,
    }


PARSE_SYSTEM_PROMPT = _register_default("parse_system", """你是专业短剧编剧、商业故事策划与 AI 影视制片。将用户提供的剧本一次性解析为结构化 JSON，供后续资产生成与分镜拆解直接使用。

════ 一、总则 ════
1. 角色、场景、道具、镜头一律使用稳定唯一名称；同一对象自始至终只用同一个名字，不得出现别名、简称、尊称混用。
2. 只输出可校验 JSON，不输出自然语言分析报告。顶层字段：project_analysis、characters、props、scenes、episodes、prop_events。
3. 所有描述字段服务于同一个标准：能否据此生成一张合格的参考图。

════ 二、角色 characters ════
4. 每项含 name、description、traits、appearance；不产出 voice_id（审核阶段补充）。
5. name 规则：项目内唯一，2-10 字，不含标点与括号；不使用别名与称谓，母亲、老王、王叔这类称呼必须收敛为一个正式名字；只出现一次、无台词、无剧情作用的路人一律不列入。
6. description：该角色在剧情中的身份与作用，供人阅读，不用于生成图片。
7. traits：可枚举的性格与身份标签（如 species_or_type、age_group、identity、personality），用数组给出。
8. appearance 是最重要的字段，必须满足全部条件：
   - 纯视觉、可直接绘制：只写看得见的东西，不写性格、命运、关系、经历
   - 自包含：脱离上下文也能画出这个人，不出现与前一致、同前、如上这类指代
   - 唯一性：写出与其他角色的可区分特征，避免一名年轻女子这类通用描述
   - 完整覆盖六要素：物种或人种、年龄感、体型身材、发型与发色、面部特征、服装与固定配饰
   - 主色明确：给出服装与主要配色的具体颜色词
   - 禁止剧情信息：不得写母亲、反派、受伤后、长大后这类需要上下文的表述
   - 不得提及其他角色名，避免参考图串味
   - 长度 60-160 字，形容词具体可画，不堆砌空泛褒义词
9. 外观会变化的角色（换装、受伤、变老、易容）：以主要出场形态写入 appearance，变化过程写进对应镜头的 costume 状态，不拆成多个角色。

════ 三、道具 props ════
10. 每项含 name（项目内唯一，用于 @名称 引用）、description、appearance。
11. name 规则同角色：唯一、无别名、无括号。
12. 只有需要独立出现在画面中的物品才列为道具。以下三类不列入：
    - 纯动作载体（端起一杯水、拿起筷子）
    - 场景固定组成部分（桥梁、树木、建筑结构、家具）
    - 无辨识度的通用物（普通桌子、路灯）
13. appearance 必须给出形状与结构、颜色、材质与表面质感、相对尺寸比例、关键辨识细节，40-120 字。
14. 状态会变化的道具（损坏、易手、消耗、变形）：在 description 写明变化与发生镜头，appearance 只描述初始完整形态。

════ 四、场景 scenes ════
15. 每项含 episode、title、description、time_note（可空）、shots 数组。
16. title 规则：地点名，2-12 字，不含人物与情节。同一地点在整部剧中只能有一个 title，不同集同一地点必须复用同名同 time_note，不得改写成近义词（马棚、马厩、马房视为同一地点，统一取一个）。
17. description 是场景基准图的唯一依据，必须满足：
    - 纯环境：只写空间、结构、材质、植被、建筑、天空、地面、光线
    - 不写人物：不出现任何角色名、称谓、人形、剪影、人群
    - 不写动作：不出现走进、站在、望着、坐下、奔跑、打斗这类动词短语
    - 不写情节与情绪：不出现离别的地方、充满回忆这类抒情表达
    - 不写剧情道具：不出现仅本场需要的可移动对象
    - 必须包含：前景、中景、远景三层空间，地面材质，光源方向与强度
    - 长度 60-180 字
18. time_note：时段与天气，如清晨·薄雾、正午·晴。同一地点的不同时段视为不同场景，title 可加时段后缀区分。

════ 五、镜头 shots ════
19. 每个 shot 含 episode、seq（同场景同集内唯一，从 1 连续）、scene_id、character_id（主说话角色名，可空）、props（道具名数组）、dialogue、dialogue_segments（每项 speaker/text/emotion/order，可选 start_time/end_time）、speaker（可空）、emotion（neutral/happy/sad/angry/fearful，取情绪曲线终点）、emotion_curve（from/trigger/turn_at/to/expression）、beats（上限 3 个，每项 action/start_pose/end_pose/duration）、start_state、end_state（六项枚举：pose/position/expression/holding/costume/injury）、image_prompt、video_prompt（可空）、shot_type、camera_angle、camera_movement、transition、shot_purpose、duration（秒，整数）、shot_kind（dialogue/narration/visual/transition）、time_jump（默认 false）、requires_prev_frame（默认 false）。
20. duration：单镜 3-10 秒。超过 10 秒的剧情必须拆成多个镜头，不得用长 duration 硬塞。
21. 对白可制作性：
    - dialogue_segments 每段 text 不超过 40 字，超过必须拆成多段
    - 多人对白必须逐段给出 speaker，不得合并成一段
    - 3 人以上快速交替对白拆成多个镜头
    - 旁白与画外音用 shot_kind=narration，speaker 置空
22. 动作节拍：复合动作拆成可独立拍摄的节拍，单镜最多 3 个，单节拍不超 2 秒。
23. 情绪设计：每镜情绪曲线给出起点情绪、触发事件、转折秒数、终点情绪与外化表现，不只标情绪词。
24. 每镜必须能具象为单一画面。抽象心理、隐喻蒙太奇、无法落地的意象必须改写为可拍摄的具体动作与场景。

════ 六、分集规划 episodes ════
25. 每项含 episode、goal、rhythm_curve、cliffhanger_type（信息缺失/危机逼近/反转揭示/关系破裂）、hook_to_next、locked（默认 false）。
26. 单集节奏：保住开场钩子、冲突升级、情绪释放、结尾悬念四节拍。

════ 七、提示词字段 ════
27. image_prompt 必须按结构化分段模板输出（PROMPTS.md §5.1），固定分段依次为：任务（任务类型，如多参考图人物一致性生成）、参考图职责（只描述实际注入的参考图：出场角色定妆图在前、所属场景基准图在后，逐行对应；不描述道具参考图，道具写进目标画面的画面描述）、美术风格（项目主视觉风格原文，全图统一，不改写）、目标画面（主体、动作、构图、景别、光线、质感，一段连续描述）、必须保持（跨图一致性 3-6 项，聚焦身份与外观）、允许变化（可省略）、禁止（人物畸形、换脸、无关元素、水印等，逐项）、输出（产物形态与清晰度）。禁止输出无分段的自由文本。
28. video_prompt 必须按状态衔接结构模板输出（PROMPTS.md §5.2），固定分段依次为：任务（生成方式 T2VA/I2VA/Ref2VA 与镜号）、参考图职责（按连线顺序以 <Picture N> 语法引用：非首镜为 <Picture 1> 上一镜头尾帧、<Picture 2> 当前镜头分镜图；每集首镜无上一镜头尾帧，仅引用 <Picture 1>）、美术风格（项目主视觉风格原文）、身份约束、开场状态（六项枚举，与 start_state 同源）、剧情与动作（本镜剧情一句话、人物动作按 beats 逐拍、镜头运动与 camera_movement 一致、画面衔接说明）、目标状态（六项枚举，与 end_state 同源）、必须保持（3-6 项）、允许变化（可省略）、禁止、对白与声音、输出（时长按 duration、画幅与质量要求）。禁止输出无分段的自由文本。

════ 八、@引用规则 ════
29. 提示词中引用角色、场景、道具一律写成 @名称，名称必须与本 JSON 中 name 或 title 完全一致，包括中间间隔号。
30. props 数组只填本镜真正参与画面或动作的道具名，未出现的不要列。

════ 九、道具生命周期事件 prop_events ════
31. prop_events 为顶层数组，记录道具在剧情中明确交代的存在与交互变化，供后续镜头连续性推导。每项含 prop（道具名）、episode、scene_title、shot_seq、event_type、evidence、holder_after、location_after。
32. event_type 只能取：introduced（首次出现）、picked_up（被拿起）、held、carried（携带或背负）、placed（放下）、transferred（易手）、dropped（掉落）、moved（位置移动）、opened、closed、modified（外观改变）、damaged（损坏）、repaired（修复）、consumed（消耗）、destroyed（毁坏）、lost（丢失）、recovered（找回）。
33. evidence 必须摘录剧本原文片段（不超过 40 字），不得编造或改写；剧本没有明确交代的变化不要产出事件。
34. 每件道具只在首次出现的镜头产出一次 introduced；进入 destroyed/consumed/lost 终态后不得再产出交互事件，除非另有 recovered。
35. scene_title 必须与 scenes 中 title 完全一致；shot_seq 为该场景内该镜头的 seq；无法定位到具体镜头时省略 shot_seq（表示该集开始即生效）。

按用户指定的集范围解析，范围外的集不落库。只输出 JSON。""")


@dataclass
class NormalizeResult:
    """归一化结果：规范化后的解析数据 + 警告与审核问题。"""
    data: dict
    parse_warnings: list = field(default_factory=list)
    review_issues: list = field(default_factory=list)


_VALID_EMOTIONS = {"neutral", "happy", "sad", "angry", "fearful"}

# 道具生命周期事件类型（与 prop_lifecycle._EVENT_TO_* 映射保持一致）
_PROP_EVENT_TYPES = {
    "introduced", "picked_up", "held", "carried", "placed", "transferred",
    "dropped", "moved", "opened", "closed", "modified", "damaged", "repaired",
    "consumed", "destroyed", "lost", "recovered",
}
_STATE_KEYS = ["pose", "position", "expression", "holding", "costume", "injury"]

# --- 解析输出硬约束（第 2 步）：把可制作性要求落到代码侧，不再只依赖提示词自觉 ---
# 只做检查与留痕，不改写模型给出的剧情数据——时长被静默夹取会打乱节奏，台词被截断会毁掉语义。
# 单镜时长上限（秒）：超过必须拆镜，否则单次生成难以稳定
_SHOT_DURATION_MAX = 10
# 单镜时长下限（秒）：低于此值说明镜头过碎
_SHOT_DURATION_MIN = 3
# 单段台词字符上限：超过会拖垮配音切分与口型对齐
_DIALOGUE_SEG_MAX_CHARS = 40


def _lint_shot(s: dict, segs: list) -> tuple[list[str], list[str]]:
    """分镜可制作性检查：返回 (警告, 审核问题)。只读，不修改传入数据。"""
    warns: list[str] = []
    issues: list[str] = []
    ep, seq = s.get("episode"), s.get("seq")
    tag = f"镜头 ep{ep}/seq{seq}"
    try:
        dur = int(s.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0
    if dur > _SHOT_DURATION_MAX:
        issues.append(
            f"{tag} duration {dur}s 超过单镜 {_SHOT_DURATION_MAX}s 上限，需拆镜后再生成"
        )
    elif dur and dur < _SHOT_DURATION_MIN:
        warns.append(f"{tag} duration {dur}s 低于 {_SHOT_DURATION_MIN}s，镜头可能过碎")
    for seg in segs or []:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if len(text) > _DIALOGUE_SEG_MAX_CHARS:
            issues.append(
                f"{tag} 单段台词 {len(text)} 字超过 {_DIALOGUE_SEG_MAX_CHARS} 字上限，需拆分为多段"
            )
    return warns, issues


def _lint_name_uniqueness(items: list, label: str, key: str = "name") -> list[str]:
    """名称收敛检查：归一化后重名说明别名未合并，会导致同一对象重复生成资产。"""
    seen: dict[str, str] = {}
    issues: list[str] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        raw = (it.get(key) or "").strip()
        if not raw:
            continue
        norm = normalize_entity_name(raw)
        if not norm:
            continue
        prev = seen.get(norm)
        if prev is not None and prev != raw:
            issues.append(
                f"{label}名称未收敛：{prev} 与 {raw} 归一化后相同，会重复生成资产"
            )
        else:
            seen.setdefault(norm, raw)
    return issues


def normalize_parse_output(raw: dict, *, episode_start: int, episode_count: int,
                           character_name_to_id: dict | None = None) -> NormalizeResult:
    """按归一化规则（PROMPTS.md §3.1，P0 强约束）处理语言模型输出。

    - 补齐 shot_kind/duration/time_jump/requires_prev_frame，补齐记入 parse_warnings
    - dialogue_segments.speaker 按角色名映射 speaker_id；映射失败保留原名记审核问题
    - emotion 缺失/非法落 neutral 记警告；范围外集过滤
    character_name_to_id：项目内已有角色名→ID 映射（新建角色由调用方先落库后回填）
    """
    result = NormalizeResult(data={})
    warnings = result.parse_warnings
    issues = result.review_issues
    char_map = character_name_to_id or {}

    ep_end = episode_start + episode_count - 1

    def _in_range(ep) -> bool:
        try:
            return episode_start <= int(ep) <= ep_end
        except (TypeError, ValueError):
            return False

    # project_analysis 原样保留
    result.data["project_analysis"] = raw.get("project_analysis") or {}
    # characters / props 过滤必填
    result.data["characters"] = [
        c for c in (raw.get("characters") or []) if isinstance(c, dict) and c.get("name")
    ]
    # 本次解析输出的角色名集合：首次解析时项目内尚无角色，speaker 只能先保留原名，
    # 由 apply_parse_result 落库后回填 speaker_id；此处不应把本批新角色误报为未匹配。
    _parsed_char_names = {
        (c.get("name") or "").strip() for c in result.data["characters"]
    }
    _parsed_char_norms = {
        normalize_entity_name(n) for n in _parsed_char_names if n
    }
    result.data["props"] = [
        p for p in (raw.get("props") or []) if isinstance(p, dict) and p.get("name")
    ]
    # prop_events（P1-2）：剧本明确交代的道具存在/交互变化，按集范围过滤并校验
    _prop_events: list[dict] = []
    for ev in raw.get("prop_events") or []:
        if not isinstance(ev, dict):
            continue
        pname = (ev.get("prop") or "").strip()
        etype = (ev.get("event_type") or "").strip()
        if not pname or etype not in _PROP_EVENT_TYPES:
            warnings.append(f"道具事件 {pname or '?'}/{etype or '?'} 名称或类型非法，已忽略")
            continue
        if not _in_range(ev.get("episode")):
            continue
        evidence = (ev.get("evidence") or "").strip()
        if not evidence:
            # source_type=script 必须有剧本原文证据，否则无法审计也不可确认
            warnings.append(f"道具事件 {pname}/{etype} 缺少剧本证据，已忽略")
            continue
        item = {
            "prop": pname,
            "episode": int(ev.get("episode")),
            "event_type": etype,
            "evidence": evidence[:200],
            "scene_title": (ev.get("scene_title") or "").strip() or None,
            "shot_seq": ev.get("shot_seq"),
            "holder_after": (ev.get("holder_after") or "").strip() or None,
            "location_after": (ev.get("location_after") or "").strip() or None,
        }
        _key = (item["prop"], item["episode"], item["scene_title"],
                item["shot_seq"], item["event_type"])
        if any((x["prop"], x["episode"], x["scene_title"], x["shot_seq"], x["event_type"]) == _key
               for x in _prop_events):
            continue
        _prop_events.append(item)
    result.data["prop_events"] = _prop_events
    # episodes 范围过滤
    result.data["episodes"] = [
        e for e in (raw.get("episodes") or []) if isinstance(e, dict) and _in_range(e.get("episode"))
    ]

    # scenes + shots 归一化
    scenes_out = []
    for scene in raw.get("scenes") or []:
        if not isinstance(scene, dict) or not _in_range(scene.get("episode")):
            continue
        shots_out = []
        for shot in scene.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            s = dict(shot)
            # shot_kind 补齐
            if not s.get("shot_kind"):
                s["shot_kind"] = "dialogue" if s.get("dialogue") else "visual"
                warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} 缺 shot_kind，按 {s['shot_kind']} 补齐")
            # duration 补齐
            if not s.get("duration"):
                s["duration"] = int(AVG_SHOT_DURATION)
                warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} 缺 duration，按默认 {int(AVG_SHOT_DURATION)}s 补齐")
            # time_jump / requires_prev_frame 补齐
            for flag in ("time_jump", "requires_prev_frame"):
                if flag not in s or s.get(flag) is None:
                    s[flag] = False
                    warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} 缺 {flag}，按 false 补齐")
            # emotion 缺失/非法落 neutral
            emo = s.get("emotion")
            if not emo or emo not in _VALID_EMOTIONS:
                s["emotion"] = "neutral"
                warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} emotion 缺失或非法，落为 neutral")
            # dialogue_segments 归一化与 speaker 映射
            segs = s.get("dialogue_segments")
            if s.get("dialogue") and not segs and (s.get("speaker") or s.get("character_id")):
                # 单人台词未给分段：由 speaker 创建单段（§3.1）
                spk = s.get("speaker") or s.get("character_id")
                segs = [{"speaker": spk, "text": s.get("dialogue"), "emotion": s["emotion"], "order": 1}]
                s["dialogue_segments"] = segs
                warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} 由 speaker 创建单段对白结构")
            if isinstance(segs, list):
                norm_segs = []
                for i, seg in enumerate(segs, 1):
                    if not isinstance(seg, dict):
                        continue
                    ns = dict(seg)
                    ns.setdefault("order", i)
                    spk_name = ns.get("speaker") or ns.get("speaker_id")
                    if spk_name and spk_name in char_map:
                        ns["speaker_id"] = char_map[spk_name]
                    elif spk_name:
                        ns["speaker_id"] = None  # 首次解析项目内角色尚未落库，确认时回填
                        if (spk_name not in _parsed_char_names
                                and normalize_entity_name(spk_name) not in _parsed_char_norms):
                            issues.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} 对白说话人 '{spk_name}' 未匹配到项目角色")
                    if ns.get("emotion") not in _VALID_EMOTIONS:
                        ns["emotion"] = s["emotion"]
                    norm_segs.append(ns)
                s["dialogue_segments"] = norm_segs
            # start_state / end_state 结构校验（六项枚举键，缺失记警告不阻断）
            for sk in ("start_state", "end_state"):
                st = s.get(sk)
                if st is not None and not isinstance(st, dict):
                    s[sk] = None
                    warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} {sk} 结构非法，置空")
            # beats 上限 3
            beats = s.get("beats")
            if isinstance(beats, list) and len(beats) > 3:
                s["beats"] = beats[:3]
                warnings.append(f"镜头 ep{s.get('episode')}/seq{s.get('seq')} beats 超上限，截断为 3")
            # 第 2 步：分镜可制作性检查（时长上限、台词长度），只留痕不改写剧情数据
            _lint_warns, _lint_issues = _lint_shot(s, s.get("dialogue_segments") or [])
            warnings.extend(_lint_warns)
            issues.extend(_lint_issues)
            shots_out.append(s)
        sc = dict(scene)
        sc["shots"] = shots_out
        scenes_out.append(sc)
    result.data["scenes"] = scenes_out

    # 第 2 步：名称收敛检查。归一化后重名意味着别名未合并，同一对象会被建成多条
    # 资产并各自生成参考图，是角色/场景一致性问题的上游源头。
    issues.extend(_lint_name_uniqueness(result.data["characters"], "角色"))
    issues.extend(_lint_name_uniqueness(result.data["props"], "道具"))
    scenes_for_lint = [
        {"title": sc.get("title")} for sc in scenes_out if isinstance(sc, dict)
    ]
    issues.extend(_lint_name_uniqueness(scenes_for_lint, "场景", key="title"))

    return result


# ── video_prompt 兜底模板（P1 外置）：LLM 未生成时按状态衔接模板拼装 ───
# 首镜版：无上一镜尾帧，仅 <Picture 1> 分镜图；
# 非首镜版：<Picture 1> 上镜尾帧 + <Picture 2> 当前分镜图
_register_default("video_prompt_first", """任务：Ref2VA 第 {episode}-{seq} 镜

【<Picture 1>：当前镜头分镜图】本镜头结束时的目标视觉效果基准：景别、构图、机位、人物外貌与关键动作。

【身份约束】参考图中的同一人物必须保持面部特征、发型、服装、体型与配饰一致，不得无故更换外观。

开场状态（按剧情与分镜图推断）：
{start_state}

剧情与动作：
本镜头剧情：{shot_purpose}
人物动作：{beats}
镜头运动：{camera_movement}
画面衔接：开场按剧情自然进入当前镜头分镜图的构图与动作。

目标状态（对齐当前镜头分镜图）：
{end_state}

必须保持：
1. 人物面部特征、发型、服装与配饰一致
2. 场景布局与光线色调连续
3. 道具位置与持有状态连续

禁止：换脸、服装跳变、道具消失、多余肢体、文字水印。

对白与声音：{sound}

输出：时长 {duration} 秒，按项目画幅，高质量输出。""")

_register_default("video_prompt_body", """任务：Ref2VA 第 {episode}-{seq} 镜

【<Picture 1>：上一镜头尾帧】本镜头开始时的连续状态基准：人物位置、站姿、朝向、表情、视线、道具与空间关系。

【<Picture 2>：当前镜头分镜图】本镜头结束时的目标视觉效果基准：景别、构图、机位、人物外貌与关键动作。

【身份约束】两张参考图中的同一人物必须保持面部特征、发型、服装、体型与配饰一致，不得无故更换外观。

开场状态（承接上一镜头尾帧 <Picture 1>）：
{start_state}

剧情与动作：
本镜头剧情：{shot_purpose}
人物动作：{beats}
镜头运动：{camera_movement}
画面衔接：开场保持 <Picture 1> 的位置、姿态、视线与道具状态，随动作自然过渡到 <Picture 2> 的构图与动作。

目标状态（对齐 <Picture 2>）：
{end_state}

必须保持：
1. 人物面部特征、发型、服装与配饰一致
2. 场景布局与光线色调连续
3. 道具位置与持有状态连续

禁止：换脸、服装跳变、道具消失、多余肢体、文字水印。

对白与声音：{sound}

输出：时长 {duration} 秒，按项目画幅，高质量输出。""")


def _assemble_video_prompt(sh: dict, is_first: bool = False) -> str:
    """video_prompt 为空时按 PROMPTS.md §5.2 状态衔接模板拼装（系统侧兜底）。

    静态模板从 workspace/prompts/video_prompt_{first,body}.md 读取（缺失回退 DEFAULT_PROMPTS），
    变量 {episode}/{seq}/{beats}/{shot_purpose}/{camera_movement}/{start_state}/{end_state}/{sound}/{duration}
    由代码运行时注入；节拍格式化与状态序列化逻辑保留在代码内。
    """
    def _state_lines(state) -> str:
        if not state:
            return "（未提供，按分镜图与剧情推断）"
        if isinstance(state, dict):
            return "\n".join(f"{k}：{v}" for k, v in state.items())
        return str(state)
    beats = sh.get("beats") or []
    beat_desc = "；".join(
        f"第{i + 1}拍 {b.get('action') or ''}（{b.get('start_pose') or ''} → {b.get('end_pose') or ''}，约{b.get('duration') or 2}秒）"
        for i, b in enumerate(beats) if isinstance(b, dict)
    ) or "（无节拍，按剧情自然表演）"
    dialogue = (sh.get("dialogue") or "").strip()
    sound = "；".join(x for x in [dialogue, sh.get("soundscape"), sh.get("bgm")] if x) or "（无对白，环境声按场景）"
    template_name = "video_prompt_first" if is_first else "video_prompt_body"
    template = get_prompt(template_name)
    return template.format(
        episode=sh.get("episode") or 1,
        seq=sh.get("seq") or 1,
        beats=beat_desc,
        shot_purpose=sh.get("shot_purpose") or "（按分镜图）",
        camera_movement=sh.get("camera_movement") or "固定",
        start_state=_state_lines(sh.get("start_state")),
        end_state=_state_lines(sh.get("end_state")),
        sound=sound,
        duration=sh.get("duration") or 10,
    )


# --- 候选解析确认后的级联落库（API.md §5 parse-version confirm / PROMPTS.md §3 集级合并规则） ---
def apply_parse_result(db, project, parse_result: dict, *, episode_start: int, episode_count: int,
                       current_scene_ids: list | None = None) -> dict:
    """把候选解析结果落库到实体表：角色/道具按名幂等，场景同名同集复用，
    镜头按 (scene, episode, seq) upsert，范围外不动；旧镜头中不在新解析内的删除。

    分集规划按锁定集规则合并：locked=true 保留原规划，locked=false 用新解析覆盖。
    返回落库统计。调用方负责事务提交。
    """
    from app.models import AssetRef, Character, Job, Prop, PropShotState, Scene, SceneEpisode, Shot
    from app.models.audio import AudioSegment
    from app.models import ids as id_gen
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stats = {"characters": 0, "props": 0, "scenes": 0, "shots_created": 0,
             "shots_updated": 0, "shots_deleted": 0}

    # 1. 角色按名幂等 upsert（精确名 → 归一化名二级匹配）
    char_map = {c.name: c for c in db.query(Character).filter(Character.project_id == project.id).all()}
    char_norm_map = {
        (c.normalized_name or normalize_entity_name(c.name)): c
        for c in char_map.values()
    }
    for c in parse_result.get("characters") or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        existing = char_map.get(name) or char_norm_map.get(normalize_entity_name(name))
        if existing:
            existing.description = c.get("description") or existing.description
            _t = c.get("traits")
            existing.traits = (", ".join(_t) if isinstance(_t, list) else _t) or existing.traits
            existing.appearance = c.get("appearance") or existing.appearance
        else:
            _t = c.get("traits")
            _traits = ", ".join(_t) if isinstance(_t, list) else _t
            new_c = Character(id=id_gen.character_id(), project_id=project.id, name=name,
                              normalized_name=normalize_entity_name(name),
                              description=c.get("description"), traits=_traits,
                              appearance=c.get("appearance"))
            db.add(new_c)
            db.flush()
            char_map[name] = new_c
            char_norm_map[new_c.normalized_name] = new_c
            stats["characters"] += 1

    # 2. 道具按名幂等 upsert（精确名 → 归一化名二级匹配）
    prop_map = {p.name: p for p in db.query(Prop).filter(Prop.project_id == project.id).all()}
    prop_norm_map = {
        (p.normalized_name or normalize_entity_name(p.name)): p
        for p in prop_map.values()
    }
    for pr in parse_result.get("props") or []:
        name = (pr.get("name") or "").strip()
        if not name:
            continue
        existing = prop_map.get(name) or prop_norm_map.get(normalize_entity_name(name))
        if existing:
            existing.description = pr.get("description") or existing.description
            existing.appearance = pr.get("appearance") or existing.appearance
        else:
            new_p = Prop(id=id_gen.prop_id(), project_id=project.id, name=name,
                         normalized_name=normalize_entity_name(name),
                         description=pr.get("description"), appearance=pr.get("appearance"))
            db.add(new_p)
            db.flush()
            prop_map[name] = new_p
            prop_norm_map[new_p.normalized_name] = new_p
            stats["props"] += 1

    # 3. 分集规划合并（locked=true 保留）
    old_plan = {e.get("episode"): e for e in (project.episode_plan or []) if isinstance(e, dict)}
    new_plan = dict(old_plan)
    for e in parse_result.get("episodes") or []:
        ep_no = e.get("episode")
        if ep_no is None:
            continue
        old_e = old_plan.get(ep_no)
        if old_e and old_e.get("locked"):
            continue  # 锁定集保留
        e = dict(e)
        e.setdefault("locked", False)
        new_plan[ep_no] = e
    project.episode_plan = [new_plan[k] for k in sorted(new_plan)]

    # 4. 场景与镜头落库（场景按「标题+集」精确匹配 → 归一化「地点+时段」二级匹配）
    scene_title_map = {}
    scene_norm_map = {}
    # 跨集索引：normalized_name → scene。同一地点在多集出现时应复用同一条 Scene，
    # 否则每集各建一份，各自生成一张基准图，同一地点会出现多张不一致的环境图。
    scene_norm_global = {}
    for s in db.query(Scene).filter(Scene.project_id == project.id).all():
        scene_title_map[(s.title, s.episode)] = s
        _s_norm = s.normalized_name or normalize_scene_name(s.title, s.time_note)
        scene_norm_map[(_s_norm, s.episode)] = s
        scene_norm_global.setdefault(_s_norm, s)
    current_ids = set(current_scene_ids or [])
    touched_shot_keys = set()  # (scene_id, episode, seq) 本次解析覆盖的镜头
    # 本次解析中已登记过的镜头键。Session 为 autoflush=False，
    # 本次新 add 的 Shot 在 flush 前查不到，同一 Scene 内若出现重复 seq
    # 会重复插入并触发 uq(scene_id, episode, seq) 冲突。
    _shot_seen = set()
    # 本次解析中已登记过的场景-集关联。Session 为 autoflush=False：
    # 新 add 的 SceneEpisode 在没有 flush 前查不到，若同一地点在同一集出现多次
    # （如『马家小院·清晨』与『马家小院·上午』命中同一个 Scene），
    # 仅靠 db.query 判空会重复插入，触发 uq(scene_id, episode) 唯一约束冲突。
    _link_seen = set()
    _seen_episodes = set()  # 已出现镜头的集号，用于判定每集首镜（无上一镜头尾帧）

    for sc in parse_result.get("scenes") or []:
        ep = sc.get("episode")
        title = (sc.get("title") or "").strip() or f"场景{sc.get('episode')}"
        _reused_cross_ep = False  # 本场景是否为跨集复用（复用时不覆盖既有描述）
        _expect_norm = normalize_scene_name(title, sc.get("time_note"))
        scene = scene_title_map.get((title, ep))
        if scene is not None and (scene.normalized_name or "") != _expect_norm:
            # 标题相同但时段不同（如『马家小院·清晨』与『马家小院·上午』）
            # 属于不同场景，不能复用同一条 Scene，否则镜头序号会互相冲突。
            scene = None
        if scene is None:
            _norm_key = (_expect_norm, int(ep or 1))
            scene = scene_norm_map.get(_norm_key)
        if scene is None:
            # 跨集复用：同一地点在其他集已建过 Scene 时复用同一条记录。
            # 否则每集各建一份，同一地点会各自生成基准图，导致环境漂移。
            # 时段不同（time_note 不同）时 normalized_name 不同，不会被误复用。
            _g_norm = normalize_scene_name(title, sc.get("time_note"))
            scene = scene_norm_global.get(_g_norm)
            if scene is not None:
                _reused_cross_ep = True
                scene_title_map[(title, int(ep or 1))] = scene
                scene_norm_map[(_g_norm, int(ep or 1))] = scene
        if scene is None:
            max_seq = (db.query(Scene.seq).filter(Scene.project_id == project.id)
                       .order_by(Scene.seq.desc()).first())
            scene = Scene(id=id_gen.scene_id(), project_id=project.id, episode=int(ep or 1),
                          seq=(max_seq[0] if max_seq else 0) + 1, title=title,
                          normalized_name=normalize_scene_name(title, sc.get("time_note")),
                          description=sc.get("description"), time_note=sc.get("time_note"))
            db.add(scene)
            db.flush()
            scene_title_map[(title, ep)] = scene
            scene_norm_map[(scene.normalized_name, scene.episode)] = scene
            stats["scenes"] += 1
        elif not _reused_cross_ep:
            # 同集重解析：描述随最新解析更新。
            # 跨集复用则不覆盖——基准图是依据首集描述生成的，覆盖会让图与依据脱节；
            # timestamp 相同说明 normalized_name 已一致，time_note 也无需改动。
            scene.description = sc.get("description") or scene.description
            scene.time_note = sc.get("time_note") or scene.time_note
        # scene_episode 关联（幂等）：先查本次解析的内存登记，再查库
        _link_key = (scene.id, int(ep or 1))
        if _link_key not in _link_seen:
            link = (db.query(SceneEpisode)
                    .filter_by(scene_id=scene.id, episode=int(ep or 1)).first())
            if link is None:
                max_se = (db.query(SceneEpisode.seq_in_episode)
                          .filter(SceneEpisode.scene_id == scene.id)
                          .order_by(SceneEpisode.seq_in_episode.desc()).first())
                db.add(SceneEpisode(id=id_gen.scene_episode_id(), scene_id=scene.id,
                                    episode=int(ep or 1),
                                    seq_in_episode=(max_se[0] if max_se else 0) + 1))
                db.flush()
            _link_seen.add(_link_key)

        # 镜头 upsert
        for sh in sc.get("shots") or []:
            seq = sh.get("seq")
            if seq is None:
                continue
            ep_i = int(sh.get("episode") or ep or 1)
            _is_first_shot = ep_i not in _seen_episodes  # 每集首镜：仅引用 <Picture 1> 分镜图
            _seen_episodes.add(ep_i)
            key = (scene.id, ep_i, int(seq))
            if key in _shot_seen:
                # 同一次解析内重复出现的同一镜头：以首次为准，避免唯一约束冲突
                continue
            _shot_seen.add(key)
            touched_shot_keys.add(key)
            # character_id 名称 → 实体 ID 二次映射（精确名优先，归一化名兜底）
            char_id = None
            cname = sh.get("character_id") or sh.get("speaker")
            if cname:
                _ch = char_map.get(cname) or char_norm_map.get(normalize_entity_name(cname))
                char_id = _ch.id if _ch is not None else None
            # 对白分段说话人回填：首次解析时角色尚未落库，speaker_id 为空；
            # 此处角色已 upsert（含本批新建），按 speaker 原名/归一化名补上实体 ID，
            # 否则配音与出场角色解析只能退回字符串名，角色关联会断。
            segments = sh.get("dialogue_segments")
            if isinstance(segments, list):
                segments = [dict(x) for x in segments if isinstance(x, dict)]
                for seg in segments:
                    if seg.get("speaker_id"):
                        continue
                    spk = seg.get("speaker")
                    if not spk:
                        continue
                    _ch = char_map.get(spk) or char_norm_map.get(normalize_entity_name(spk))
                    if _ch is not None:
                        seg["speaker_id"] = _ch.id
            _shot_props = sh.get("props")
            if _shot_props is not None and not isinstance(_shot_props, list):
                _shot_props = None
            fields = dict(
                character_id=char_id,
                # 剧本标注的本镜涉及道具（道具名数组）：生命周期推导与连续性校验依赖此字段，
                # 长期未落库会让「本镜涉及道具」判断全部退化为提示词正文启发式
                props=_shot_props,
                dialogue=sh.get("dialogue"),
                dialogue_segments=segments,
                shot_kind=sh.get("shot_kind") or "visual",
                image_prompt=sh.get("image_prompt"),
                video_prompt=sh.get("video_prompt") or _assemble_video_prompt(sh, _is_first_shot),
                shot_type=sh.get("shot_type"), camera_angle=sh.get("camera_angle"),
                camera_movement=sh.get("camera_movement"), transition=sh.get("transition"),
                shot_purpose=sh.get("shot_purpose"), emotion=sh.get("emotion") or "neutral",
                duration=int(sh.get("duration") or 10),
                time_jump=bool(sh.get("time_jump")),
                requires_prev_frame=bool(sh.get("requires_prev_frame")),
                start_state=sh.get("start_state"), end_state=sh.get("end_state"),
                beats=sh.get("beats"), emotion_curve=sh.get("emotion_curve"),
            )
            existing = (db.query(Shot).filter_by(scene_id=scene.id, episode=ep_i, seq=int(seq))
                        .first())
            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
                stats["shots_updated"] += 1
            else:
                db.add(Shot(id=id_gen.shot_id(), scene_id=scene.id, episode=ep_i,
                            seq=int(seq), status="pending", **fields))
                db.flush()
                stats["shots_created"] += 1

    # 5. 删除本次解析范围内、但新解析不再包含的旧镜头（仅限当前生效版本的场景）
    ep_end = episode_start + episode_count - 1
    if current_ids:
        stale_shots = (
            db.query(Shot).join(Scene, Shot.scene_id == Scene.id)
            .filter(Scene.project_id == project.id,
                    Scene.id.in_(current_ids),
                    Shot.episode >= episode_start, Shot.episode <= ep_end)
            .all()
        )
        for s in stale_shots:
            if (s.scene_id, s.episode, s.seq) not in touched_shot_keys:
                # 解除引用：Job.shot_id 置 NULL 保留任务审计记录；
                # AssetRef.shot_id 不可空，随旧镜头一并删除；
                # prop_shot_state 是可重建的派生状态（prop_lifecycle 先清后写），随镜头删除；
                # audio_segment 是绑定该镜头的时间轴规划，镜头已不存在则规划失效，随镜头删除
                #（音频 Asset 与媒体文件保留，不丢素材）
                db.query(Job).filter(Job.shot_id == s.id).update({"shot_id": None})
                db.query(AssetRef).filter(AssetRef.shot_id == s.id).delete()
                db.query(PropShotState).filter(PropShotState.shot_id == s.id).delete(
                    synchronize_session=False)
                db.query(AudioSegment).filter(AudioSegment.shot_id == s.id).delete(
                    synchronize_session=False)
                db.delete(s)
                stats["shots_deleted"] += 1

    # 6. 资产定义定稿落库：名称、占位路径、提示词与参考依赖一次性写入
    #    名称与路径一经生成不再改动，后续生成/替换只更新内容与状态
    from app.services.asset_plan import build_asset_plans
    plan_stats = build_asset_plans(db, project, episode_start=episode_start,
                                   episode_count=episode_count)
    stats["asset_plans"] = plan_stats

    # 7. 道具生命周期状态推导（P0 自动接入）：基于已确认生命周期事件
    #    重算本批次集数内每镜状态（prop_shot_state）并写集末快照（prop_episode_snapshot）。
    #    推导可重建（先清后写），事件表为唯一事实来源；无事件的道具不写基线快照，
    #    避免 not_introduced 被提示词组装误当作剧情判断。
    from app.models import Prop as _Prop
    from app.models import PropLifecycleEvent as _PLE
    from app.services.prop_lifecycle import add_event, derive_shot_states, write_episode_snapshot

    # 7.1 剧本道具事件落库（只追加，重复解析按「同镜同类型同来源」幂等跳过）
    prop_events_created = 0
    for ev in parse_result.get("prop_events") or []:
        _pname = ev.get("prop") or ""
        _prop = prop_map.get(_pname) or prop_norm_map.get(normalize_entity_name(_pname))
        if _prop is None:
            continue
        _ep = int(ev.get("episode") or 1)
        _shot_id = None
        _title = ev.get("scene_title")
        _scene = None
        if _title:
            _scene = scene_title_map.get((_title, _ep)) or scene_title_map.get((_title, int(_ep)))
        if _scene is not None and ev.get("shot_seq") is not None:
            try:
                _sh = (db.query(Shot)
                       .filter_by(scene_id=_scene.id, episode=_ep, seq=int(ev["shot_seq"]))
                       .first())
            except (TypeError, ValueError):
                _sh = None
            _shot_id = _sh.id if _sh is not None else None
        _dup = (db.query(_PLE)
                .filter_by(project_id=project.id, prop_id=_prop.id, episode=_ep,
                           shot_id=_shot_id, event_type=ev["event_type"], source_type="script")
                .first())
        if _dup is not None:
            continue
        add_event(db, project.id, _prop.id, episode=_ep, event_type=ev["event_type"],
                  source_type="script", shot_id=_shot_id,
                  source_evidence=ev.get("evidence"),
                  holder_after=ev.get("holder_after"),
                  location_after=ev.get("location_after"))
        prop_events_created += 1
    stats["prop_events"] = prop_events_created

    episodes = list(range(episode_start, episode_start + episode_count))
    derived_props = 0
    for p in db.query(_Prop).filter(_Prop.project_id == project.id).all():
        if derive_shot_states(db, project.id, p.id, episodes=episodes):
            derived_props += 1
        for ep in episodes:
            write_episode_snapshot(db, project.id, p.id, ep)
    stats["prop_lifecycle_props"] = derived_props
    return stats
