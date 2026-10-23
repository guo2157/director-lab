"""剧本评估与解析服务：PROMPTS.md §2/§3 规范的实现。

- estimate：集数与时长估算。结合对白、旁白、动作、停顿、转场的时长模型，
  字符数/语速仅作缺少结构信息时的 fallback（API.md §5 estimate 约定）
- parse：调用语言模型一次性解析剧本，按归一化规则（P0 强约束）补齐字段
- 归一化：补齐 shot_kind/duration/time_jump/requires_prev_frame、
  speaker_id 按角色名映射（失败保留原名并记审核问题）、emotion 缺失落 neutral 记警告
所有外部调用（语言模型）在短数据库事务之外执行（AGENTS.md §3-4）。
"""
import re
from dataclasses import dataclass, field

# --- 估算算法常量（API.md §5：记录算法/提示词版本及主要假设） ---
ESTIMATE_ALGORITHM_VERSION = "est-v1.0"
# 语速模型（字/秒）：对白正常语速，旁白稍快，动作/视觉按节拍估算
DIALOGUE_CHARS_PER_SECOND = 5.0
NARRATION_CHARS_PER_SECOND = 7.0
# 单镜平均时长与转场/停顿附加（秒）
AVG_SHOT_DURATION = 10.0
TRANSITION_OVERHEAD_PER_SCENE = 2.0
# 单集目标时长（秒），来自项目 settings.episode_duration_target 默认值
DEFAULT_EPISODE_DURATION = 90
DEFAULT_SHOTS_PER_EPISODE = 9

_DIALOGUE_RE = re.compile(r'["“](.*?)["”]|[:：]\s*["“](.*?)["”]', re.DOTALL)


def estimate_script(text: str, *, episode_duration_target: int = DEFAULT_EPISODE_DURATION,
                    max_episodes_per_batch: int = 10) -> dict:
    """评估剧本可制作集数。返回评估对象（API.md §5 estimate 响应结构）。

    估算模型：区分对白（引号内）与非对白（旁白/动作）时长，加场景转场开销，
    汇总为总时长区间后按目标单集时长切分集数。字符数/语速为 fallback 输入之一。
    """
    char_count = len(text)
    # 提取对白（中英文引号）
    dialogue_chars = 0
    for m in _DIALOGUE_RE.finditer(text):
        seg = m.group(1) or m.group(2) or ""
        dialogue_chars += len(seg.strip())
    non_dialogue_chars = max(0, char_count - dialogue_chars)
    # 时长估算：对白按对白语速，非对白（旁白/动作描述）按旁白语速 + 视觉节拍冗余
    dialogue_seconds = dialogue_chars / DIALOGUE_CHARS_PER_SECOND
    narration_seconds = non_dialogue_chars / NARRATION_CHARS_PER_SECOND
    # 场景数估算（按换行分段近似），每场景加转场/停顿开销
    scene_breaks = len([l for l in re.split(r'\n+', text) if l.strip()])
    estimated_scenes = max(1, min(scene_breaks, char_count // 200 + 1))
    transition_seconds = estimated_scenes * TRANSITION_OVERHEAD_PER_SCENE
    total_seconds = dialogue_seconds + narration_seconds + transition_seconds
    # 集数：按目标单集时长向上取整，至少 1 集
    estimated_episodes = max(1, -(-int(total_seconds) // episode_duration_target))
    # 每集分解
    episode_breakdown = []
    remaining = total_seconds
    for ep in range(1, estimated_episodes + 1):
        ep_duration = min(episode_duration_target, remaining)
        remaining -= ep_duration
        episode_breakdown.append({
            "episode": ep,
            "estimated_duration": round(ep_duration, 1),
            "estimated_shots": max(1, round(ep_duration / AVG_SHOT_DURATION)),
        })
    exceeds = estimated_episodes > max_episodes_per_batch
    suggestion = None
    if exceeds:
        suggestion = f"剧本预计 {estimated_episodes} 集，超过单次制作上限 {max_episodes_per_batch} 集，建议裁剪剧本或分批制作"
    return {
        "algorithm_version": ESTIMATE_ALGORITHM_VERSION,
        "char_count": char_count,
        "dialogue_char_estimate": dialogue_chars,
        "estimated_total_duration": round(total_seconds, 1),
        "estimated_episodes": estimated_episodes,
        "episode_breakdown": episode_breakdown,
        "exceeds_batch_limit": exceeds,
        "suggestion": suggestion,
    }


# --- 剧本解析系统提示词（PROMPTS.md §2 模板浓缩，输出 §3 结构 JSON） ---
PARSE_SYSTEM_PROMPT = """你是专业短剧编剧、商业故事策划和影视剧本顾问。将用户提供的剧本一次性解析为结构化 JSON，供后续资产生成与分镜拆解直接使用。

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

按用户指定的集范围解析，范围外的集不落库。只输出 JSON。"""


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


# --- 候选解析确认后的级联落库（API.md §5 parse-version confirm / PROMPTS.md §3 集级合并规则） ---
def apply_parse_result(db, project, parse_result: dict, *, episode_start: int, episode_count: int,
                       current_scene_ids: list | None = None) -> dict:
    """把候选解析结果落库到实体表：角色/道具按名幂等，场景同名同集复用，
    镜头按 (scene, episode, seq) upsert，范围外不动；旧镜头中不在新解析内的删除。

    分集规划按锁定集规则合并：locked=true 保留原规划，locked=false 用新解析覆盖。
    返回落库统计。调用方负责事务提交。
    """
    from app.models import Character, Prop, Scene, SceneEpisode, Shot
    from app.models import ids as id_gen
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stats = {"characters": 0, "props": 0, "scenes": 0, "shots_created": 0,
             "shots_updated": 0, "shots_deleted": 0}

    # 1. 角色按名幂等 upsert
    char_map = {c.name: c for c in db.query(Character).filter(Character.project_id == project.id).all()}
    for c in parse_result.get("characters") or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        existing = char_map.get(name)
        if existing:
            existing.description = c.get("description") or existing.description
            existing.traits = c.get("traits") or existing.traits
            existing.appearance = c.get("appearance") or existing.appearance
        else:
            new_c = Character(id=id_gen.character_id(), project_id=project.id, name=name,
                              description=c.get("description"), traits=c.get("traits"),
                              appearance=c.get("appearance"))
            db.add(new_c)
            db.flush()
            char_map[name] = new_c
            stats["characters"] += 1

    # 2. 道具按名幂等 upsert
    prop_map = {p.name: p for p in db.query(Prop).filter(Prop.project_id == project.id).all()}
    for pr in parse_result.get("props") or []:
        name = (pr.get("name") or "").strip()
        if not name:
            continue
        existing = prop_map.get(name)
        if existing:
            existing.description = pr.get("description") or existing.description
            existing.appearance = pr.get("appearance") or existing.appearance
        else:
            new_p = Prop(id=id_gen.prop_id(), project_id=project.id, name=name,
                         description=pr.get("description"), appearance=pr.get("appearance"))
            db.add(new_p)
            db.flush()
            prop_map[name] = new_p
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

    # 4. 场景与镜头落库
    scene_title_map = {}
    for s in db.query(Scene).filter(Scene.project_id == project.id).all():
        scene_title_map[(s.title, s.episode)] = s
    current_ids = set(current_scene_ids or [])
    touched_shot_keys = set()  # (scene_id, episode, seq) 本次解析覆盖的镜头

    for sc in parse_result.get("scenes") or []:
        ep = sc.get("episode")
        title = (sc.get("title") or "").strip() or f"场景{sc.get('episode')}"
        scene = scene_title_map.get((title, ep))
        if scene is None:
            max_seq = (db.query(Scene.seq).filter(Scene.project_id == project.id)
                       .order_by(Scene.seq.desc()).first())
            scene = Scene(id=id_gen.scene_id(), project_id=project.id, episode=int(ep or 1),
                          seq=(max_seq[0] if max_seq else 0) + 1, title=title,
                          description=sc.get("description"), time_note=sc.get("time_note"))
            db.add(scene)
            db.flush()
            scene_title_map[(title, ep)] = scene
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
                image_prompt=sh.get("image_prompt"), video_prompt=sh.get("video_prompt"),
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
                db.delete(s)
                stats["shots_deleted"] += 1
    return stats
