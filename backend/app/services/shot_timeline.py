"""Shot 时间轴访问器：提供统一的 timeline 读取、派生 beats、预算校验。

设计原则（见 director_lab_full_pipeline_refactor_execution_plan.md §3.3）：
- timeline 是唯一事实源
- beats 降级为从 timeline 派生的只读视图
- 旧数据（timeline 为空）回退读 beats
- 各消费方通过本模块的 shot_timeline() 统一访问，不再直接读 beats
"""
from __future__ import annotations

from typing import Any

# --- 切镜原因枚举（方案 §3.2）---
CUT_REASONS = frozenset({
    "scene_change",
    "time_jump",
    "new_visual_focus",
    "reaction_shot",
    "emotional_turn",
    "speaker_overload",
    "capacity_limit",
    "camera_setup_change",
    "continuity_break",
    "episode_end",
})

# --- 默认预算值（通过配置覆盖）---
DEFAULT_MAX_SPEAKERS = 2          # 单镜说话人上限
HARD_MAX_SPEAKERS = 3             # 硬上限（参考音频槽位）
DEFAULT_MAX_CHARACTERS = 3        # 单镜角色数
DEFAULT_MAX_PROP_TRANSFERS = 2    # 镜内道具易手次数
DEFAULT_MAX_TIMELINE_UNITS = 6    # timeline 单元数上限
DEFAULT_TARGET_DURATION_RANGE = (6, 12)  # 默认推荐时长区间
DEFAULT_CHARS_PER_SECOND = 5.5    # 台词字数/秒（待 P0 实测标定）


def shot_timeline(shot: Any) -> list[dict]:
    """统一访问器：返回镜内时间轴。

    优先读 shot.timeline（新列），为空时回退读 shot.beats（旧数据兼容）。
    始终返回 list[dict]。

    使用方式：
        from app.services.shot_timeline import shot_timeline
        tl = shot_timeline(shot)
        for unit in tl:
            print(unit["action"], unit.get("start"), unit.get("end"))
    """
    # 新列 timeline 优先
    if shot.timeline and isinstance(shot.timeline, list):
        return shot.timeline
    # 旧数据回退：beats → timeline 适配
    if shot.beats and isinstance(shot.beats, list):
        return _beats_to_timeline(shot.beats, shot.duration or 0)
    return []


def timeline_to_beats(timeline: list[dict]) -> list[dict]:
    """从 timeline 派生 beats（保持旧消费方的兼容性）。

    beats 结构保留 {action, start_pose, end_pose, duration}，
    从 timeline 单元的 action / state_after / end-start 推导。
    """
    result = []
    for unit in timeline:
        start = unit.get("start", 0)
        end = unit.get("end", start)
        dur = max(1, int(round(end - start)))
        state_after = unit.get("state_after", {})
        beat = {
            "action": unit.get("action", ""),
            "start_pose": "",
            "end_pose": state_after.get("position", "") if isinstance(state_after, dict) else str(state_after),
            "duration": dur,
        }
        result.append(beat)
    return result


def _beats_to_timeline(beats: list[dict], total_duration: int) -> list[dict]:
    """旧 beats 数组 → timeline 格式（均分时间轴，因为旧 beats 无精确起止）。

    这不是精确转换——旧 beats 每 beat 的 duration 是 LLM 估算值，
    将其直接当作时间轴会产生偏差。这里做均分处理作为兼容降级。
    """
    n = len(beats)
    if n == 0:
        return []
    if total_duration <= 0:
        total_duration = n * 2  # fallback: 每 beat 2 秒
    step = total_duration / n
    result = []
    for i, b in enumerate(beats):
        result.append({
            "id": f"tl_legacy_{i + 1}",
            "start": round(i * step, 1),
            "end": round((i + 1) * step, 1),
            "action": b.get("action", "") if isinstance(b, dict) else str(b),
            "characters": [],
            "props": [],
            "dialogue_refs": [],
            "_source": "beats_legacy",
        })
    return result


def validate_timeline(
    timeline: list[dict],
    duration: int,
    dialogue_segments: list[dict] | None = None,
    known_characters: set[str] | None = None,
    known_props: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """校验 timeline（方案附录 A）。

    Returns:
        (warnings, issues): issues 为阻断级问题，warnings 为提醒。
    """
    warnings: list[str] = []
    issues: list[str] = []

    if not timeline:
        return warnings, issues

    # 1. 起止连续校验
    for i, unit in enumerate(timeline):
        start = unit.get("start")
        end_ = unit.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end_, (int, float)):
            issues.append(f"timeline[{i}] 缺少 start/end")
            continue
        if start >= end_:
            issues.append(f"timeline[{i}] start({start}) >= end({end_})")

    if not issues:
        sorted_tl = sorted(timeline, key=lambda u: u.get("start", 0))
        for i in range(len(sorted_tl) - 1):
            gap = sorted_tl[i + 1]["start"] - sorted_tl[i]["end"]
            if abs(gap) > 0.5:
                warnings.append(f"timeline 单元 {i} → {i + 1} 存在 {gap:.1f}s 间隙")
        first_start = sorted_tl[0].get("start", 0) if sorted_tl else 0
        if abs(first_start) > 0.5:
            warnings.append(f"timeline 首项 start={first_start}，期望 0")
        if sorted_tl:
            last_end = sorted_tl[-1].get("end", 0)
            if abs(last_end - duration) > 0.5:
                warnings.append(f"timeline 末项 end={last_end}，duration={duration}（容差 ±0.5s）")

    # 2. action 不能是"说话/开口"
    for unit in timeline:
        action = unit.get("action", "")
        # 同一个单元内既有对白又有动作是允许的（dialogue_refs 挂载），
        # 但 action 本身不应只是"说话"类动词
        if action.strip() in ("说话", "开口", "问话", "回答", "说"):
            warnings.append(f"timeline action '{action}' 应由 dialogue_segments 表达")

    # 3. characters 校验
    if known_characters:
        for i, unit in enumerate(timeline):
            chars = unit.get("characters", [])
            for c in chars:
                if c not in known_characters:
                    warnings.append(f"timeline[{i}] 角色 '{c}' 不在项目已知角色中")

    # 4. props 校验
    if known_props:
        for i, unit in enumerate(timeline):
            props = unit.get("props", [])
            for p in props:
                if p not in known_props:
                    warnings.append(f"timeline[{i}] 道具 '{p}' 不在项目已知道具中")

    # 5. dialogue_refs 校验
    if dialogue_segments:
        valid_orders = {ds.get("order") for ds in dialogue_segments if isinstance(ds, dict)}
        for i, unit in enumerate(timeline):
            refs = unit.get("dialogue_refs", [])
            for ref in refs:
                if ref not in valid_orders:
                    issues.append(f"timeline[{i}] dialogue_refs[{ref}] 不存在于 dialogue_segments 中")
        # 每句对白恰好挂在一个时间轴单元上
        assigned_orders = set()
        for unit in timeline:
            assigned_orders.update(unit.get("dialogue_refs", []))
        unassigned = valid_orders - assigned_orders
        if unassigned:
            warnings.append(f"对白 order {sorted(unassigned)} 未挂载到任何 timeline 单元")
        overassigned = assigned_orders - valid_orders
        if overassigned:
            issues.append(f"timeline 引用了不存在的对白 order: {sorted(overassigned)}")

    # 6. 单元数上限
    if len(timeline) > DEFAULT_MAX_TIMELINE_UNITS:
        warnings.append(f"timeline 单元数 {len(timeline)} > {DEFAULT_MAX_TIMELINE_UNITS}，建议拆镜 (capacity_limit)")

    return warnings, issues


def check_budget(
    timeline: list[dict],
    duration: int,
    dialogue_segments: list[dict] | None = None,
    character_count: int = 0,
    prop_transfer_count: int = 0,
    max_speakers: int = DEFAULT_MAX_SPEAKERS,
    hard_max_speakers: int = HARD_MAX_SPEAKERS,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
    max_prop_transfers: int = DEFAULT_MAX_PROP_TRANSFERS,
    max_duration: int = 15,
    min_duration: int = 6,
    chars_per_second: float = DEFAULT_CHARS_PER_SECOND,
) -> dict:
    """预算器（方案 §3.4）。

    返回检查结果字典，含 passed/budget_checks/violations。
    超限项给出 cut_reason 建议。
    """
    checks: list[dict] = []
    violations: list[str] = []

    # 时长
    if duration > max_duration:
        violations.append("capacity_limit:duration")
        checks.append({"check": "duration", "value": duration, "limit": max_duration, "passed": False})
    elif duration < min_duration:
        checks.append({"check": "duration", "value": duration, "limit": min_duration,
                       "passed": True, "warning": "duration_below_recommended"})
    else:
        checks.append({"check": "duration", "value": duration, "limit": max_duration, "passed": True})

    # 说话人（从 dialogue_segments 统计）
    speakers: set[str] = set()
    if dialogue_segments:
        for ds in dialogue_segments:
            if isinstance(ds, dict) and ds.get("speaker"):
                speakers.add(ds["speaker"])
    speaker_count = len(speakers)
    if speaker_count > hard_max_speakers:
        violations.append("speaker_overload")
        checks.append({"check": "speakers", "value": speaker_count, "limit": hard_max_speakers, "passed": False})
    elif speaker_count > max_speakers:
        checks.append({"check": "speakers", "value": speaker_count, "limit": max_speakers,
                       "passed": True, "warning": "over_default"})
    else:
        checks.append({"check": "speakers", "value": speaker_count, "limit": max_speakers, "passed": True})

    # 台词容量（字数 ≤ 时长 × chars_per_second）
    total_chars = 0
    if dialogue_segments:
        total_chars = sum(len(ds.get("text", "")) for ds in dialogue_segments if isinstance(ds, dict))
    char_budget = duration * chars_per_second
    if total_chars > char_budget:
        violations.append("capacity_limit:dialogue_length")
        checks.append({"check": "dialogue_chars", "value": total_chars, "limit": int(char_budget), "passed": False})
    else:
        checks.append({"check": "dialogue_chars", "value": total_chars, "limit": int(char_budget), "passed": True})

    # 角色数
    if character_count > max_characters:
        violations.append("capacity_limit:characters")
        checks.append({"check": "characters", "value": character_count, "limit": max_characters, "passed": False})
    else:
        checks.append({"check": "characters", "value": character_count, "limit": max_characters, "passed": True})

    # 道具易手
    if prop_transfer_count > max_prop_transfers:
        violations.append("capacity_limit:prop_transfers")
        checks.append({"check": "prop_transfers", "value": prop_transfer_count, "limit": max_prop_transfers, "passed": False})
    else:
        checks.append({"check": "prop_transfers", "value": prop_transfer_count, "limit": max_prop_transfers, "passed": True})

    # 时间轴单元数
    unit_count = len(timeline)
    if unit_count > DEFAULT_MAX_TIMELINE_UNITS:
        violations.append("capacity_limit:timeline_units")
        checks.append({"check": "timeline_units", "value": unit_count, "limit": DEFAULT_MAX_TIMELINE_UNITS, "passed": False})
    else:
        checks.append({"check": "timeline_units", "value": unit_count, "limit": DEFAULT_MAX_TIMELINE_UNITS, "passed": True})

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "budget_checks": checks,
    }
