"""Prop Lifecycle Engine（道具生命周期引擎）。

对应 PROP_LIFECYCLE_PLAN.md §3：
- 状态机执行与分镜状态推导（derive_shot_states）
- 连续性规则校验（validate_continuity）
- 集末状态快照（write_episode_snapshot，跨集连续性依据）
- 事件提交入口（add_event，只追加不覆盖）

写入控制：所有道具状态变更仅经本模块（服务层唯一入口），
Agent/接口只能通过这里提交事件或触发推导，不允许多方直写状态表。

通用性约束：本模块不含任何具体故事/道具名称的规则；
状态来源为剧情事件（script/confirmed 人工事件），不由生图模型决定。
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Prop, PropEpisodeSnapshot, PropLifecycleEvent, PropShotState, Scene, Shot

# 存在状态终态集合：进入后仅 recovered/人工事件可回退
_TERMINAL_EXISTENCE = {"destroyed", "consumed", "lost"}

# 事件类型到状态转移的通用映射（不存在具体道具名称）
_EVENT_TO_EXISTENCE = {
    "introduced": "active",
    "recovered": "active",
    "destroyed": "destroyed",
    "consumed": "consumed",
    "lost": "lost",
}
_EVENT_TO_INTERACTION = {
    "picked_up": "held",
    "held": "held",
    "carried": "carried",
    "placed": "placed",
    "transferred": "transferred",
    "dropped": "placed",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def add_event(db: Session, project_id: str, prop_id: str, *,
              episode: int, event_type: str, source_type: str,
              shot_id: str | None = None, source_evidence: str | None = None,
              holder_before: str | None = None, holder_after: str | None = None,
              location_before: str | None = None, location_after: str | None = None,
              physical_before: dict | None = None, physical_after: dict | None = None,
              confidence: float | None = None, review_status: str | None = None,
              supersedes_id: str | None = None,
              analysis_version: int = 1) -> PropLifecycleEvent:
    """提交生命周期事件（只追加）。source_type=script 时 evidence 必填。"""
    if source_type not in ("script", "llm_inference", "human"):
        raise ValueError(f"invalid_source_type: {source_type}")
    if source_type == "script" and not source_evidence:
        raise ValueError("script_event_requires_evidence")
    if review_status is None:
        # llm_inference 默认待确认，确认后才参与状态推导；script/human 视为已确认
        review_status = "pending" if source_type == "llm_inference" else "confirmed"
    ev = PropLifecycleEvent(
        project_id=project_id, prop_id=prop_id, episode=episode, shot_id=shot_id,
        event_type=event_type, source_type=source_type, source_evidence=source_evidence,
        holder_before=holder_before, holder_after=holder_after,
        location_before=location_before, location_after=location_after,
        physical_before=physical_before, physical_after=physical_after,
        confidence=confidence, review_status=review_status,
        supersedes_id=supersedes_id, analysis_version=analysis_version,
    )
    db.add(ev)
    db.flush()
    return ev


def _ordered_shots(db: Session, project_id: str, episode: int) -> list[Shot]:
    """按集内播放顺序返回镜头（scene_episode.seq_in_episode + shot.seq）。"""
    from app.models import SceneEpisode
    return (
        db.query(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .join(SceneEpisode, (SceneEpisode.scene_id == Scene.id)
              & (SceneEpisode.episode == Shot.episode))
        .filter(Scene.project_id == project_id, Shot.episode == episode)
        .order_by(SceneEpisode.seq_in_episode, Shot.seq)
        .all()
    )


def _effective_events(db: Session, project_id: str, prop_id: str) -> list[PropLifecycleEvent]:
    """取参与推导的有效事件：script/human 直接生效；llm_inference 需 confirmed；
    已被人工修正（supersedes 指向）的事件不再生效。按集与镜头顺序排序。"""
    events = (
        db.query(PropLifecycleEvent)
        .filter(
            PropLifecycleEvent.project_id == project_id,
            PropLifecycleEvent.prop_id == prop_id,
            PropLifecycleEvent.supersedes_id.is_(None),
        )
        .all()
    )
    return [
        e for e in events
        if e.source_type in ("script", "human") or e.review_status == "confirmed"
    ]


def _shot_order_key(shot: Shot) -> tuple[int, int]:
    return (shot.episode, shot.seq)


def derive_shot_states(db: Session, project_id: str, prop_id: str,
                       episodes: list[int] | None = None,
                       analysis_version: int = 1) -> list[PropShotState]:
    """按镜头顺序推导道具状态，写入 prop_shot_state（可重建，先清后写）。

    推导规则（通用）：
    - 引入前 not_introduced；终态（destroyed/consumed/lost）后保持，仅 recovered/人工可回退
    - 无事件镜头默认继承上一镜（continuity_from_shot 记录链路）
    - time_jump=true 的镜头存在性置 unknown（不能机械继承）
    - 场景切换时：carried/held 道具跟随持有者；placed 道具留在原位置并置 offscreen
      （画外不等于消失）
    """
    from app.models import Character
    shots = _ordered_shots(db, project_id, 1 if episodes is None else episodes[0]) \
        if episodes else []
    if episodes:
        shots = []
        for ep in episodes:
            shots.extend(_ordered_shots(db, project_id, ep))
    if not shots:
        return []

    # 清空旧快照（推导结果可重建，历史通过 state_version/事件表追溯）
    db.query(PropShotState).filter(
        PropShotState.project_id == project_id,
        PropShotState.prop_id == prop_id,
        PropShotState.shot_id.in_([s.id for s in shots]),
    ).delete(synchronize_session=False)

    events = _effective_events(db, project_id, prop_id)
    events_by_shot: dict[str | None, list[PropLifecycleEvent]] = {}
    for e in events:
        events_by_shot.setdefault(e.shot_id, []).append(e)
    # shot_id 为空的引入类事件视作最早生效（如剧本层面首提）
    pre_events = events_by_shot.get(None, [])

    char_name_to_id = {
        c.name: c.id
        for c in db.query(Character).filter(Character.project_id == project_id).all()
    }
    char_id_to_name = {v: k for k, v in char_name_to_id.items()}

    state = {
        "existence": "not_introduced",
        "interaction": "unassigned",
        "holder_id": None,
        "position": None,
        "physical": None,
    }
    prev_shot: Shot | None = None
    out: list[PropShotState] = []

    def apply_event(ev: PropLifecycleEvent, shot: Shot) -> None:
        new_existence = _EVENT_TO_EXISTENCE.get(ev.event_type)
        if new_existence:
            state["existence"] = new_existence
        new_interaction = _EVENT_TO_INTERACTION.get(ev.event_type)
        if new_interaction:
            state["interaction"] = new_interaction
        if ev.holder_after is not None:
            state["holder_id"] = char_name_to_id.get(ev.holder_after, ev.holder_after)
        if ev.location_after is not None:
            state["position"] = ev.location_after
        if ev.physical_after is not None:
            state["physical"] = ev.physical_after
        if ev.event_type == "recovered":
            state["interaction"] = "unassigned"

    introduced = bool(pre_events) or state["existence"] != "not_introduced"
    for ev in pre_events:
        apply_event(ev, shots[0])

    for shot in shots:
        shot_events = sorted(
            events_by_shot.get(shot.id, []),
            key=lambda e: (e.created_at or _now(), e.id),
        )
        scene_changed = (
            prev_shot is not None and prev_shot.scene_id != shot.scene_id
        )
        # 场景切换：placed 道具留在原位置（画外不等于消失）；跟随持有者的道具位置随人走
        # 说明：placed 道具场景切换时不更新 position（保留原场景位置），
        #       visibility 由下方规则置为 offscreen，不误判为消失

        for ev in shot_events:
            apply_event(ev, shot)
            introduced = introduced or ev.event_type == "introduced"

        existence = state["existence"]
        if existence == "not_introduced" and shot.props:
            # 剧本标注本镜涉及该道具但无引入事件：标记 unknown 待确认（不能凭空断言出现）
            existence = "unknown"
        if shot.time_jump and existence == "active" and not shot_events:
            # 剧情时间跳转且无事件：不能机械继承，标记待确认
            existence = "unknown"

        visibility = "unknown"
        reference_required = False
        prompt_text = shot.image_prompt or ""
        prop = db.get(Prop, prop_id)
        prop_name = prop.name if prop else None
        if existence in ("not_introduced", "destroyed", "consumed"):
            # 不存在：不传参考图，禁止生成（提示词组装阶段读取后写负向约束）
            visibility = "offscreen" if existence != "not_introduced" else "unknown"
        elif prop_name and prop_name in prompt_text:
            visibility = "visible"
            reference_required = True
        elif existence == "active" and state["interaction"] in ("carried", "held"):
            # 被携带/手持的道具：持有者入镜（镜头主角色，或提示词/对白提及持有者）
            # → 默认 visible 并注入参考图，不依赖每个分镜提示词都恰好写到道具名。
            # 持有者未入镜 → 道具跟随持有者画外（offscreen ≠ absent）
            holder_name = char_id_to_name.get(state["holder_id"])
            holder_in_shot = state["holder_id"] is not None and (
                shot.character_id == state["holder_id"]
                or (holder_name is not None and holder_name in prompt_text)
                or (holder_name is not None and holder_name in (shot.dialogue or ""))
            )
            if holder_in_shot:
                visibility = "visible"
                reference_required = True
            else:
                visibility = "offscreen"
        elif existence == "active":
            # placed 等其他情况：道具持续存在但当前镜头未拍到 → 画外（offscreen ≠ absent）
            # placed 且场景切换时留在原位置，不误判为消失
            visibility = "offscreen"

        s = PropShotState(
            project_id=project_id, prop_id=prop_id, shot_id=shot.id,
            episode=shot.episode,
            existence_state=existence,
            interaction_state=state["interaction"],
            holder_id=state["holder_id"],
            position=state["position"],
            physical_state=state["physical"],
            visibility=visibility,
            reference_required=reference_required,
            continuity_from_shot=prev_shot.id if prev_shot else None,
            continuity_event_ids=[e.id for e in shot_events],
            state_version=analysis_version,
        )
        db.add(s)
        out.append(s)
        prev_shot = shot

    db.flush()
    return out


def write_episode_snapshot(db: Session, project_id: str, prop_id: str,
                           episode: int, analysis_version: int = 1) -> PropEpisodeSnapshot | None:
    """写集末快照：取该集最后一个镜头的状态作为下一集推导初始状态。"""
    # 按集内播放顺序取最后一个镜头的状态（不能按记录 ID 排序，ID 与镜头顺序无关）
    from app.models import SceneEpisode
    last = (
        db.query(PropShotState)
        .join(Shot, PropShotState.shot_id == Shot.id)
        .join(Scene, Shot.scene_id == Scene.id)
        .outerjoin(SceneEpisode, (SceneEpisode.scene_id == Scene.id)
                   & (SceneEpisode.episode == Shot.episode))
        .filter(
            PropShotState.project_id == project_id,
            PropShotState.prop_id == prop_id,
            PropShotState.episode == episode,
        )
        .order_by(
            SceneEpisode.seq_in_episode.desc(), Shot.seq.desc(),
        )
        .first()
    )
    if last is None:
        return None
    snap = (
        db.query(PropEpisodeSnapshot)
        .filter(
            PropEpisodeSnapshot.project_id == project_id,
            PropEpisodeSnapshot.prop_id == prop_id,
            PropEpisodeSnapshot.episode == episode,
        )
        .first()
    )
    final_state = {
        "existence_state": last.existence_state,
        "interaction_state": last.interaction_state,
        "holder_id": last.holder_id,
        "position": last.position,
        "physical_state": last.physical_state,
        "last_shot_id": last.shot_id,
    }
    if snap is None:
        snap = PropEpisodeSnapshot(
            project_id=project_id, prop_id=prop_id, episode=episode,
            final_state=final_state, analysis_version=analysis_version,
        )
        db.add(snap)
    else:
        snap.final_state = final_state
        snap.analysis_version = analysis_version
    db.flush()
    return snap


def validate_continuity(db: Session, project_id: str, prop_id: str) -> list[dict]:
    """连续性校验：输出问题清单（不直接修改数据）。

    检查项（通用规则）：
    - 未引入即出现（剧本标注 props 但无 introduced）
    - 无拾取/持有事件即被持有
    - 终态后又在镜头 props 中出现
    - 持有者与镜头主角色关系缺失（提示词未体现）
    """
    issues: list[dict] = []
    states = (
        db.query(PropShotState)
        .join(Shot, PropShotState.shot_id == Shot.id)
        .filter(PropShotState.project_id == project_id,
                PropShotState.prop_id == prop_id)
        .all()
    )
    shot_map = {s.id: s for s in db.query(Shot).filter(
        Shot.id.in_([st.shot_id for st in states])).all()} if states else {}
    for st in states:
        shot = shot_map.get(st.shot_id)
        shot_props = shot.props if shot else None
        if st.existence_state == "not_introduced" and shot_props:
            issues.append({
                "code": "prop_seen_before_introduced",
                "shot_id": st.shot_id, "prop_id": prop_id,
                "message": "镜头涉及该道具但缺少 introduced 引入事件，需人工确认",
                "suggestion": "补充 introduced 事件或将该镜头道具标注移除",
            })
        if st.existence_state in _TERMINAL_EXISTENCE and shot_props:
            issues.append({
                "code": "prop_after_terminal",
                "shot_id": st.shot_id, "prop_id": prop_id,
                "message": f"道具已处于 {st.existence_state} 终态但镜头仍涉及该道具",
                "suggestion": "确认剧情是否有 recovered/道具更换事件",
            })
        if st.interaction_state in ("held", "carried") and not st.holder_id:
            issues.append({
                "code": "held_without_holder",
                "shot_id": st.shot_id, "prop_id": prop_id,
                "message": "道具被持有但无持有者记录",
                "suggestion": "补充 picked_up/transferred 事件的 holder_after",
            })
    return issues
