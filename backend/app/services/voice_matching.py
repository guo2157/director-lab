"""角色音色智能匹配服务（2026-11-07）。

设计约束（需求文档 §5、§7.2）：
- LLM 只产出结构化声音画像（voice_profile），不决定分数、不创造 speaker_id
- 匹配评分由程序基于 VoiceLibrary 结构化标签计算
  （权重：性别 20% / 听感年龄 20% / 音色特点 25% / 性格 20% / 说话风格 15%）
- 全局分配：主角优先获得高辨识度音色，重要配角尽量与主角区分，
  次要/群众角色允许复用；角色数 > 音色数时按惩罚项自然复用
- 已锁定（is_locked）的绑定绝不参与自动分配；人工指定（source=manual）
  且已确认（approved）的绑定默认排除（include_locked 显式开启才参与）
- speaker_id 必须来自 VoiceLibrary（源头为 workflow meta voice_values），
  任何路径都不允许写入不存在的 speaker_id
"""
import json
from datetime import datetime, timezone

from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import Session

from app.models import ids
from app.models.audio import AudioSegment, VoiceLibrary, VoiceProfile
from app.models.narrative import Character
from app.services.prompt_store import _register_default, get_prompt

_VOICE_PROFILE_SYSTEM = _register_default("voice_profile_system", (
    "你是影视角色声音画像分析师。根据角色设定输出结构化声音画像 JSON，"
    "不虚构角色设定中没有的信息。输出格式："
    "{\"characters\":[{\"character_id\":\"…\","
    "\"gender_presentation\":\"male|female|unknown\","
    "\"perceived_age\":\"child|young|middle|mature|elder|unknown\","
    "\"pitch\":\"high|medium_high|medium|medium_low|low|unknown\","
    "\"timbre\":[…],\"personality\":[…],\"speaking_style\":[…],"
    "\"speaking_speed\":\"slow|medium|fast\",\"energy\":\"low|medium|high\","
    "\"role_importance\":\"main|supporting|minor|extra\"}]}。"
    "规则：剧本明确指定的声音性别/年龄/音调优先；性格只作参考（勇敢不代表低沉，"
    "温柔不代表女声）；不确定的字段输出 unknown，不要猜。"
))

# 角色重要性权重（LLM 画像 role_importance + 对白数量共同决定分配顺序）
_IMPORTANCE_WEIGHT = {"main": 4, "supporting": 3, "minor": 2, "extra": 1}

# 匹配维度权重（需求 §5.2）
_WEIGHTS = {
    "gender": 0.20,
    "age": 0.20,
    "timbre": 0.25,
    "personality": 0.20,
    "speaking_style": 0.15,
}

# 全局分配复用惩罚：更高优先级角色已占用该音色时的扣分
_REUSE_PENALTY = 15.0

# 画像字段合法值（白名单校验，非法一律置 unknown，不采纳 LLM 的自由发挥）
_GENDERS = {"male", "female", "unknown"}
_AGES = {"child", "young", "middle", "mature", "elder", "unknown"}
_PITCHES = {"high", "medium_high", "medium", "medium_low", "low", "unknown"}
_IMPORTANCES = {"main", "supporting", "minor", "extra"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_tags(value, limit: int = 6) -> list[str]:
    """标签数组规范化：仅保留非空字符串，截断到 limit。"""
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if isinstance(x, str) and str(x).strip()][:limit]


def _profile_from_llm(raw: dict, character_id: str) -> dict:
    """LLM 输出 → 受控结构化画像（白名单校验，非法值置 unknown）。"""
    gender = str(raw.get("gender_presentation") or "unknown").lower()
    age = str(raw.get("perceived_age") or "unknown").lower()
    pitch = str(raw.get("pitch") or "unknown").lower()
    importance = str(raw.get("role_importance") or "minor").lower()
    return {
        "character_id": character_id,
        "role_importance": importance if importance in _IMPORTANCES else "minor",
        "gender_presentation": gender if gender in _GENDERS else "unknown",
        "perceived_age": age if age in _AGES else "unknown",
        "pitch": pitch if pitch in _PITCHES else "unknown",
        "timbre": _norm_tags(raw.get("timbre")),
        "personality": _norm_tags(raw.get("personality")),
        "speaking_style": _norm_tags(raw.get("speaking_style")),
        "speaking_speed": str(raw.get("speaking_speed") or "medium"),
        "energy": str(raw.get("energy") or "medium"),
    }


def build_voice_profiles(db: Session, project_id: str, characters: list[Character]) -> dict[str, dict]:
    """LLM 生成角色声音画像。返回 character_id → profile。

    LLM 调用失败或未配置时降级：画像置 unknown（gender 由 LLM 不可得时不猜），
    评分仍可运行但区分度下降；角色名与描述照实提交，不虚构设定。
    """
    from app.core.config import get_settings
    settings = get_settings()
    profiles: dict[str, dict] = {}
    if not settings.llm_api_key or not settings.llm_base_url:
        for ch in characters:
            profiles[ch.id] = _profile_from_llm({}, ch.id)
        return profiles

    from app.integrations.llm import LLMClient, LLMError
    char_lines = []
    for ch in characters:
        char_lines.append(json.dumps({
            "character_id": ch.id,
            "name": ch.name,
            "description": (ch.description or "")[:300],
            "traits": (ch.traits or "")[:200],
        }, ensure_ascii=False))
    system = get_prompt("voice_profile_system")
    user = "角色列表：\n" + "\n".join(char_lines)
    try:
        client = LLMClient()
        result = client.chat_json(system, user)
        rows = result.get("characters") if isinstance(result, dict) else None
        rows = rows if isinstance(rows, list) else []
        by_id = {r.get("character_id"): r for r in rows if isinstance(r, dict)}
    except LLMError:
        # LLM 失败不阻断匹配：降级为 unknown 画像（程序评分仍可执行）
        by_id = {}
    for ch in characters:
        profiles[ch.id] = _profile_from_llm(by_id.get(ch.id) or {}, ch.id)
    return profiles


def score_voice(voice: VoiceLibrary, profile: dict) -> dict:
    """程序计算单个角色画像与单个音色的维度分与总分（0-100）。"""
    # 性别：画像明确且与音色一致 100；画像 unknown 60；明确但不一致 0
    g = profile.get("gender_presentation") or "unknown"
    gender_score = 100.0 if g == voice.gender_presentation else (60.0 if g == "unknown" else 0.0)

    # 听感年龄：相等 100；任一 unknown 50；不等 20
    a = profile.get("perceived_age") or "unknown"
    if a != "unknown" and voice.perceived_age != "unknown":
        age_score = 100.0 if a == voice.perceived_age else 20.0
    else:
        age_score = 50.0

    def _tag_score(profile_tags: list[str], voice_tags: list | None) -> float:
        vt = [str(x) for x in (voice_tags or [])]
        if not profile_tags or not vt:
            return 50.0
        hit = len(set(profile_tags) & set(vt))
        return min(100.0, 100.0 * hit / len(profile_tags))

    timbre_score = _tag_score(profile.get("timbre") or [], voice.timbre)
    personality_score = _tag_score(profile.get("personality") or [], voice.personality_tags)
    style_score = _tag_score(profile.get("speaking_style") or [], voice.speaking_styles)

    total = (
        gender_score * _WEIGHTS["gender"]
        + age_score * _WEIGHTS["age"]
        + timbre_score * _WEIGHTS["timbre"]
        + personality_score * _WEIGHTS["personality"]
        + style_score * _WEIGHTS["speaking_style"]
    )
    return {
        "total": round(total, 1),
        "gender": gender_score,
        "age": age_score,
        "timbre": round(timbre_score, 1),
        "personality": round(personality_score, 1),
        "speaking_style": round(style_score, 1),
    }


def _match_reason(voice: VoiceLibrary, profile: dict, scores: dict) -> str:
    parts = []
    if scores["gender"] >= 100:
        parts.append("性别特征匹配")
    if scores["age"] >= 100:
        parts.append(f"听感年龄相符（{voice.perceived_age}）")
    hits = set(profile.get("timbre") or []) & {str(x) for x in (voice.timbre or [])}
    if hits:
        parts.append(f"音色特点涵盖：{'、'.join(sorted(hits))}")
    phits = set(profile.get("personality") or []) & {str(x) for x in (voice.personality_tags or [])}
    if phits:
        parts.append(f"性格贴合：{'、'.join(sorted(phits))}")
    if not parts:
        parts.append("综合维度评分最优")
    return "；".join(parts) + "。"


def _dialogue_counts(db: Session, project_id: str) -> dict[str, int]:
    """角色对白片段数量（程序化重要性依据之一，不虚构）。"""
    rows = db.execute(
        select(AudioSegment.character_id, sa_func.count(AudioSegment.id))
        .where(
            AudioSegment.project_id == project_id,
            AudioSegment.track_type == "dialogue",
            AudioSegment.character_id.isnot(None),
        )
        .group_by(AudioSegment.character_id)
    ).all()
    return {cid: int(n) for cid, n in rows}


def run_match(
    db: Session,
    project_id: str,
    character_ids: list[str] | None = None,
    include_locked: bool = False,
) -> dict:
    """批量自动匹配主入口：画像 → 评分 → 全局分配 → 持久化候选结果。

    - 只写 VoiceProfile 匹配字段，不改 approved（匹配结果待用户试听确认）
    - 锁定角色绝不覆盖；人工指定且已确认的绑定默认排除
    - 返回逐角色分配结果与跳过清单
    """
    q = select(Character).where(Character.project_id == project_id).order_by(Character.created_at)
    if character_ids:
        q = q.where(Character.id.in_(character_ids))
    characters = list(db.execute(q).scalars().all())
    if not characters:
        return {"matched": [], "skipped": [], "total": 0}

    voices = list(
        db.execute(
            select(VoiceLibrary)
            .where(VoiceLibrary.enabled.is_(True))
            .order_by(VoiceLibrary.sort_order)
        ).scalars().all()
    )
    if not voices:
        raise ValueError("系统音色库为空，请先执行 scripts/seed_voice_library.py")

    # 排除规则：锁定绝不参与；人工指定且已确认默认排除（include_locked 显式开启才参与）
    existing_map: dict[str, VoiceProfile] = {}
    if characters:
        rows = db.execute(
            select(VoiceProfile).where(
                VoiceProfile.project_id == project_id,
                VoiceProfile.character_id.in_([c.id for c in characters]),
            )
        ).scalars().all()
        for v in rows:
            existing_map.setdefault(v.character_id, v)

    matched_chars: list[Character] = []
    skipped: list[dict] = []
    for ch in characters:
        cur = existing_map.get(ch.id)
        if cur is not None and cur.is_locked:
            skipped.append({"character_id": ch.id, "name": ch.name, "reason": "已锁定"})
            continue
        if (
            not include_locked
            and cur is not None
            and cur.source == "manual"
            and cur.approved
        ):
            skipped.append({"character_id": ch.id, "name": ch.name, "reason": "人工指定且已确认"})
            continue
        matched_chars.append(ch)

    if not matched_chars:
        return {"matched": [], "skipped": skipped, "total": 0}

    profiles = build_voice_profiles(db, project_id, matched_chars)
    dlg_counts = _dialogue_counts(db, project_id)

    # 分配顺序：重要性权重 × 10000 + 对白数量（同重要性时对白多者优先）
    def _priority(ch: Character) -> int:
        p = profiles[ch.id]
        imp = _IMPORTANCE_WEIGHT.get(p.get("role_importance") or "minor", 2)
        return imp * 10000 + dlg_counts.get(ch.id, 0)

    ordered = sorted(matched_chars, key=_priority, reverse=True)

    # 贪心全局分配：已占用音色按占用者优先级施加复用惩罚；extra 角色鼓励复用（无惩罚）
    occupied: dict[str, list[tuple[int, str]]] = {}  # voice_code -> [(priority, character_id)]
    results: list[dict] = []
    for ch in ordered:
        profile = profiles[ch.id]
        prio = _priority(ch)
        is_extra = (profile.get("role_importance") or "minor") == "extra"
        scored = []
        for v in voices:
            s = score_voice(v, profile)
            penalty = 0.0
            if not is_extra:
                owners = occupied.get(v.voice_code) or []
                penalty = _REUSE_PENALTY * len(owners)
            scored.append({
                "voice": v,
                "scores": s,
                "final": round(s["total"] - penalty, 1),
                "penalty": penalty,
            })
        scored.sort(key=lambda x: (-x["final"], x["voice"].sort_order))
        best = scored[0]
        v = best["voice"]
        occupied.setdefault(v.voice_code, []).append((prio, ch.id))
        alts = [
            {
                "voice_library_id": x["voice"].id,
                "voice_code": x["voice"].voice_code,
                "name": x["voice"].name,
                "speaker_id": x["voice"].speaker_id,
                "match_score": x["scores"]["total"],
                "match_reason": _match_reason(x["voice"], profile, x["scores"]),
            }
            for x in scored[1:4]
        ]
        results.append({
            "character": ch,
            "profile": profile,
            "voice": v,
            "scores": best["scores"],
            "penalty": best["penalty"],
            "alternatives": alts,
            "reason": _match_reason(v, profile, best["scores"]),
        })

    # 持久化：更新/创建 VoiceProfile 匹配字段（approved 保持原状，待用户确认）
    for r in results:
        ch, v, profile = r["character"], r["voice"], r["profile"]
        vp = existing_map.get(ch.id)
        if vp is None:
            vp = VoiceProfile(
                id=ids.voice_profile_id(),
                project_id=project_id,
                character_id=ch.id,
                created_at=_naive_now(),
                updated_at=_naive_now(),
            )
            db.add(vp)
        vp.voice_id = v.speaker_id
        vp.voice_name = v.name
        vp.voice_library_id = v.id
        vp.match_score = r["scores"]["total"]
        vp.match_reason = r["reason"]
        vp.alternatives = r["alternatives"]
        vp.voice_profile_json = profile
        vp.source = "ai"
        vp.is_locked = False
        vp.updated_at = _naive_now()
    db.commit()

    return {
        "matched": [
            {
                "character_id": r["character"].id,
                "character_name": r["character"].name,
                "voice_code": r["voice"].voice_code,
                "voice_name": r["voice"].name,
                "speaker_id": r["voice"].speaker_id,
                "match_score": r["scores"]["total"],
                "match_reason": r["reason"],
                "reused": r["penalty"] > 0,
                "alternatives": r["alternatives"],
            }
            for r in results
        ],
        "skipped": skipped,
        "total": len(results),
    }
