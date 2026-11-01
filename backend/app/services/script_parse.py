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


PARSE_SYSTEM_PROMPT = _register_default("parse_system", """你是专业短剧编剧、商业故事策划和影视剧本顾问。将用户提供的剧本一次性解析为结构化 JSON，供后续资产生成与分镜拆解直接使用。

创作与输出规则：
1. 角色、场景、道具、镜头一律使用稳定名称/标识；每镜给出参考资产、生成方式、时长、对白、音效与审核条件。
2. 只输出可校验 JSON，不输出自然语言分析报告。顶层字段：project_analysis、characters、props、scenes、episodes。
3. project_analysis 含 summary（核心创意与卖点）、theme（核心主题）、highlights（主要看点）、foreshadowing（伏笔清单，每项 setup_episode/audience_guess/reveal_episode/payoff）。
4. characters 每项含 name、description、traits、appearance；不产出 voice_id（审核阶段补充）。
5. props 每项含 name（项目内唯一，用于 @名称 引用）、description、appearance。
6. scenes 每项含 episode、title、description、time_note（可空）、shots 数组。
7. 每个 shot 含 episode、seq（同场景同集内唯一）、scene_id、character_id（主说话角色名，可空）、props（道具名数组）、dialogue、dialogue_segments（每项 speaker/text/emotion/order，可选 start_time/end_time；多人对白必须逐段给出 speaker）、speaker（可空）、emotion（neutral/happy/sad/angry/fearful，取情绪曲线终点）、emotion_curve（from/trigger/turn_at/to/expression）、beats（上限 3 个，每项 action/start_pose/end_pose/duration）、start_state、end_state（六项枚举）、image_prompt、video_prompt（可空）、shot_type、camera_angle、camera_movement、transition、shot_purpose、duration（秒，整数）、shot_kind（dialogue/narration/visual/transition）、time_jump（默认 false）、requires_prev_frame（默认 false）。
8. 情绪设计：每镜情绪曲线给出起点情绪、触发事件、转折秒数、终点情绪与外化表现，不只标情绪词。
9. 单集节奏：保住开场钩子、冲突升级、情绪释放、结尾悬念四节拍。episodes 每项含 episode、goal、rhythm_curve、cliffhanger_type（信息缺失/危机逼近/反转揭示/关系破裂）、hook_to_next、locked（默认 false）。
10. 动作节拍：复合动作拆成可独立拍摄节拍，单镜最多 3 个，单节拍不超 2 秒。
11. image_prompt 必须按结构化分段模板输出（PROMPTS.md §5.1），固定分段依次为：任务（任务类型，如多参考图人物一致性生成）、参考图职责（仅描述实际注入的参考图：出场角色定妆图在前、所属场景基准图在后，逐行对应；禁止描述道具参考图，道具一律写入目标画面的画面描述中）、美术风格（固定为项目主视觉风格原文，见用户消息中的【项目创作配置】的主视觉风格，全图统一，不得改写或替换）、目标画面（主体、动作、构图、景别、光线、质感，一段连续描述）、必须保持（跨图一致性要求，逐项列出 3–6 项，聚焦身份与外观）、允许变化（可自由发挥项，可省略）、禁止（人物畸形、换脸、无关元素、水印等，逐项列出）、输出（产物形态与清晰度要求）。禁止输出无分段的自由文本。
12. video_prompt 必须按状态衔接结构模板输出（PROMPTS.md §5.2），固定分段依次为：任务（生成方式 T2VA/I2VA/Ref2VA 与镜号）、参考图职责（按连线顺序以 <Picture N> 语法引用：非首镜为 <Picture 1> 上一镜头尾帧、<Picture 2> 当前镜头分镜图；每集首镜无上一镜头尾帧，仅引用 <Picture 1> 当前镜头分镜图）、美术风格（固定为项目主视觉风格原文，见用户消息中的【项目创作配置】的主视觉风格，全图统一，不得改写或替换）、身份约束（同一人物面部特征、发型、服装、体型与配饰一致；道具掉落、损毁、易手等状态变化必须明确道具当前位置与时机，禁止与剧情矛盾的默认穿戴）、开场状态（六项枚举：人物位置/身体姿态/视线方向/道具状态/情绪状态/空间与机位，与 start_state 同源）、剧情与动作（本镜头剧情一句话、人物动作按 beats 逐拍、情绪触发与走向、镜头运动与 camera_movement 一致、画面衔接从 <Picture 1> 过渡到 <Picture 2>（首镜从开场状态过渡到 <Picture 1> 分镜图））、目标状态（六项枚举，与 end_state 同源）、必须保持（3–6 项，聚焦身份、服装、道具与空间关系）、允许变化（可省略）、禁止（换脸、服装跳变、道具消失、多余肢体、文字水印等）、对白与声音（台词与 dialogue 一致、环境声、动作音效、背景音乐）、输出（时长按 duration、画幅与质量要求）。禁止输出无分段的自由文本。

按用户指定的集范围解析，范围外的集不落库。只输出 JSON。""")


@dataclass
class NormalizeResult:
    """归一化结果：规范化后的解析数据 + 警告与审核问题。"""
    data: dict
    parse_warnings: list = field(default_factory=list)
    review_issues: list = field(default_factory=list)


_VALID_EMOTIONS = {"neutral", "happy", "sad", "angry", "fearful"}
_STATE_KEYS = ["pose", "position", "expression", "holding", "costume", "injury"]


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
    result.data["props"] = [
        p for p in (raw.get("props") or []) if isinstance(p, dict) and p.get("name")
    ]
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
                        ns["speaker_id"] = None  # 映射失败保留原名，产生审核问题
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
            shots_out.append(s)
        sc = dict(scene)
        sc["shots"] = shots_out
        scenes_out.append(sc)
    result.data["scenes"] = scenes_out
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
    from app.models import AssetRef, Character, Job, Prop, Scene, SceneEpisode, Shot
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
    for s in db.query(Scene).filter(Scene.project_id == project.id).all():
        scene_title_map[(s.title, s.episode)] = s
        scene_norm_map[(s.normalized_name or normalize_scene_name(s.title, s.time_note), s.episode)] = s
    current_ids = set(current_scene_ids or [])
    touched_shot_keys = set()  # (scene_id, episode, seq) 本次解析覆盖的镜头
    _seen_episodes = set()  # 已出现镜头的集号，用于判定每集首镜（无上一镜头尾帧）

    for sc in parse_result.get("scenes") or []:
        ep = sc.get("episode")
        title = (sc.get("title") or "").strip() or f"场景{sc.get('episode')}"
        scene = scene_title_map.get((title, ep))
        if scene is None:
            _norm_key = (normalize_scene_name(title, sc.get("time_note")), int(ep or 1))
            scene = scene_norm_map.get(_norm_key)
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
        else:
            scene.description = sc.get("description") or scene.description
            scene.time_note = sc.get("time_note") or scene.time_note
        # scene_episode 关联（幂等）
        link = (db.query(SceneEpisode)
                .filter_by(scene_id=scene.id, episode=int(ep or 1)).first())
        if link is None:
            max_se = (db.query(SceneEpisode.seq_in_episode)
                      .filter(SceneEpisode.scene_id == scene.id)
                      .order_by(SceneEpisode.seq_in_episode.desc()).first())
            db.add(SceneEpisode(id=id_gen.scene_episode_id(), scene_id=scene.id,
                                episode=int(ep or 1),
                                seq_in_episode=(max_se[0] if max_se else 0) + 1))

        # 镜头 upsert
        for sh in sc.get("shots") or []:
            seq = sh.get("seq")
            if seq is None:
                continue
            ep_i = int(sh.get("episode") or ep or 1)
            _is_first_shot = ep_i not in _seen_episodes  # 每集首镜：仅引用 <Picture 1> 分镜图
            _seen_episodes.add(ep_i)
            key = (scene.id, ep_i, int(seq))
            touched_shot_keys.add(key)
            # character_id 名称 → 实体 ID 二次映射
            char_id = None
            cname = sh.get("character_id") or sh.get("speaker")
            if cname and cname in char_map:
                char_id = char_map[cname].id
            fields = dict(
                character_id=char_id, dialogue=sh.get("dialogue"),
                dialogue_segments=sh.get("dialogue_segments"),
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
                # AssetRef.shot_id 不可空，随旧镜头一并删除
                db.query(Job).filter(Job.shot_id == s.id).update({"shot_id": None})
                db.query(AssetRef).filter(AssetRef.shot_id == s.id).delete()
                db.delete(s)
                stats["shots_deleted"] += 1

    # 6. 资产定义定稿落库：名称、占位路径、提示词与参考依赖一次性写入
    #    名称与路径一经生成不再改动，后续生成/替换只更新内容与状态
    from app.services.asset_plan import build_asset_plans
    plan_stats = build_asset_plans(db, project, episode_start=episode_start,
                                   episode_count=episode_count)
    stats["asset_plans"] = plan_stats
    return stats
