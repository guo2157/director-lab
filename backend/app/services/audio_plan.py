"""音频规划服务：从剧本解析结果生成结构化音频片段规划。

设计约束（docs/agent/audio-module-audit-report-20261106.md G1/G6/G8）：
- 对白/旁白从 Shot 结构化数据确定性生成，不伪造 LLM 未产出的内容
- BGM 按场景聚合为跨镜头连续片段（cross_shot=True）
- SFX/环境声从 Shot.soundscape 生成（解析未产出时留空，不虚构数量）
- H3 原生音频片段由 clip 生成后检测登记（executor 调用 register_native_audio）
- 规划幂等：同 parse 版本重复生成时先清理旧规划再重建
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ids
from app.models.audio import AudioSegment, VoiceProfile
from app.models.narrative import Character, Scene, Shot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _char_name_map(db: Session, project_id: str) -> dict[str, str]:
    """角色名 → ID 映射（含归一化名）。"""
    rows = db.execute(
        select(Character.id, Character.name, Character.normalized_name)
        .where(Character.project_id == project_id)
    ).all()
    mapping: dict[str, str] = {}
    for cid, name, norm in rows:
        if name:
            mapping[name] = cid
        if norm:
            mapping.setdefault(norm, cid)
    return mapping


def build_audio_plan(db: Session, project_id: str, episode: int) -> dict:
    """为指定集生成音频规划片段（确定性，从 Shot 数据推导）。

    返回统计：{dialogue: n, narration: n, bgm: n, sfx: n, ambient: n, total: n}
    幂等：删除该集已有非 native_audio 片段后重建（native_audio 绑定视频版本，不重建）。
    """
    shots = list(db.execute(
        select(Shot)
        .join(Scene, Shot.scene_id == Scene.id)
        .where(Shot.episode == episode)
        .order_by(Scene.id, Shot.seq)
    ).scalars().all())
    if not shots:
        return {"episode": episode, "dialogue": 0, "narration": 0, "bgm": 0,
                "sfx": 0, "ambient": 0, "total": 0, "note": "该集无镜头数据"}

    name_map = _char_name_map(db, project_id)
    # 角色音色确认状态预取（音色确认调度闸口，2026-11-07）：
    # 角色 exists 默认音色且 approved → 对白可直接生成（pending）；
    # 否则 → waiting_voice_confirmation，音色确认后由 confirm 接口批量放行
    from sqlalchemy import select as _select
    _approved_chars: set[str] = {
        vp.character_id
        for vp in db.execute(
            _select(VoiceProfile).where(
                VoiceProfile.project_id == project_id,
                VoiceProfile.is_default == True,  # noqa: E712
                VoiceProfile.approved == True,  # noqa: E712
            )
        ).scalars().all()
    }

    # 幂等清理：仅清非 native_audio 的规划片段
    old = db.execute(
        select(AudioSegment).where(
            AudioSegment.project_id == project_id,
            AudioSegment.episode == episode,
            AudioSegment.track_type != "native_audio",
        )
    ).scalars().all()
    for seg in old:
        db.delete(seg)

    counts = {"dialogue": 0, "narration": 0, "bgm": 0, "sfx": 0, "ambient": 0}

    # 1. 对白片段：从 dialogue_segments 逐段生成
    for shot in shots:
        segs = shot.dialogue_segments or []
        if not segs and shot.dialogue and shot.shot_kind == "dialogue":
            spk = shot.character_id or None
            segs = [{"speaker": spk, "text": shot.dialogue, "emotion": shot.emotion, "order": 1}]
        for seg in segs:
            if not isinstance(seg, dict) or not (seg.get("text") or "").strip():
                continue
            speaker = seg.get("speaker") or ""
            char_id = name_map.get(speaker) or (shot.character_id if speaker == shot.character_id else None)
            # 音色确认闸口：有角色的对白必须音色已确认才能进入生成队列；
            # 旁白/无角色片段不受此闸口限制（走项目级 narrator_voice 配置）
            if char_id and char_id not in _approved_chars:
                _seg_status = "waiting_voice_confirmation"
            else:
                _seg_status = "pending"
            db.add(AudioSegment(
                id=ids.audio_segment_id(),
                project_id=project_id, episode=episode, shot_id=shot.id,
                track_type="dialogue", segment_type="character_dialogue",
                character_id=char_id, text=seg.get("text", ""),
                emotion=seg.get("emotion") or shot.emotion,
                sequence=int(seg.get("order") or 0) or None,
                generation_status=_seg_status, review_status="pending",
                version=1,
            ))
            counts["dialogue"] += 1

    # 2. 旁白片段：shot_kind=narration 的镜头（可跨镜头，V1 按镜头登记，cross_shot 标记由前端按相邻镜头聚合展示）
    for shot in shots:
        if shot.shot_kind == "narration" and (shot.dialogue or "").strip():
            db.add(AudioSegment(
                id=ids.audio_segment_id(),
                project_id=project_id, episode=episode, shot_id=shot.id,
                track_type="narration", segment_type="voiceover",
                text=shot.dialogue,
                emotion=shot.emotion,
                generation_status="pending", review_status="pending",
                cross_shot=True, version=1,
            ))
            counts["narration"] += 1

    # 3. BGM 片段：按场景聚合（bgm 字段或场景情绪建议），跨镜头连续
    scene_bgm: dict[str, AudioSegment] = {}
    for shot in shots:
        scene = db.get(Scene, shot.scene_id)
        if scene is None:
            continue
        key = shot.scene_id
        if key not in scene_bgm:
            desc = (shot.bgm or "").strip() or f"场景『{scene.title}』氛围配乐（按情绪规划）"
            seg = AudioSegment(
                id=ids.audio_segment_id(),
                project_id=project_id, episode=episode, shot_id=shot.id,
                track_type="bgm", segment_type="music",
                text=desc,
                generation_status="pending", review_status="pending",
                cross_shot=True, version=1,
                note="按场景聚合的跨镜头连续 BGM",
            )
            scene_bgm[key] = seg
            db.add(seg)
            counts["bgm"] += 1

    # 4. SFX / 环境声：从 soundscape 字段生成（未产出则不虚构）
    for shot in shots:
        sc = (shot.soundscape or "").strip()
        if not sc:
            continue
        # 简单启发：含『环境/氛围/风/雨/街道』等关键词归环境声，其余归 SFX
        ambient_kw = ("环境", "氛围", "风", "雨", "街道", "鸟", "虫", "海浪", "人群")
        is_ambient = any(k in sc for k in ambient_kw)
        db.add(AudioSegment(
            id=ids.audio_segment_id(),
            project_id=project_id, episode=episode, shot_id=shot.id,
            track_type="ambient" if is_ambient else "sfx",
            segment_type="ambience" if is_ambient else "effect",
            text=sc,
            generation_status="pending", review_status="pending",
            cross_shot=is_ambient, version=1,
        ))
        counts["ambient" if is_ambient else "sfx"] += 1

    db.commit()
    total = sum(counts.values())
    return {"episode": episode, **counts, "total": total}


def get_audio_plan(db: Session, project_id: str, episode: int) -> dict:
    """获取音频规划：按轨道类型分组返回片段列表与统计。"""
    segs = list(db.execute(
        select(AudioSegment)
        .where(AudioSegment.project_id == project_id, AudioSegment.episode == episode)
        .order_by(AudioSegment.track_type, AudioSegment.start_time_ms, AudioSegment.created_at)
    ).scalars().all())
    by_track: dict[str, list[dict]] = {}
    for seg in segs:
        by_track.setdefault(seg.track_type, []).append(_segment_out(seg))
    counts = {t: len(v) for t, v in by_track.items()}
    return {
        "project_id": project_id,
        "episode": episode,
        "tracks": by_track,
        "counts": counts,
        "total": len(segs),
    }


def get_audio_timeline(db: Session, project_id: str, episode: int) -> dict:
    """获取音频时间轴：含片段时间位置、资产引用与状态（供最终合成页）。"""
    segs = list(db.execute(
        select(AudioSegment)
        .where(AudioSegment.project_id == project_id, AudioSegment.episode == episode)
        .order_by(AudioSegment.start_time_ms)
    ).scalars().all())
    return {
        "project_id": project_id,
        "episode": episode,
        "items": [_segment_out(seg) for seg in segs],
        "total": len(segs),
    }


def _segment_out(seg: AudioSegment) -> dict:
    return {
        "id": seg.id,
        "project_id": seg.project_id,
        "episode": seg.episode,
        "shot_id": seg.shot_id,
        "track_type": seg.track_type,
        "segment_type": seg.segment_type,
        "character_id": seg.character_id,
        "text": seg.text,
        "voice_profile_id": seg.voice_profile_id,
        "emotion": seg.emotion,
        "instruct": seg.instruct,
        "sequence": seg.sequence,
        "speed": seg.speed,
        "start_time_ms": seg.start_time_ms,
        "duration_ms": seg.duration_ms,
        "audio_asset_id": seg.audio_asset_id,
        "video_asset_id": seg.video_asset_id,
        "generation_status": seg.generation_status,
        "review_status": seg.review_status,
        "cross_shot": seg.cross_shot,
        "version": seg.version,
        "note": seg.note,
    }


def register_native_audio(db: Session, project_id: str, episode: int, shot_id: str,
                          video_asset_id: str, has_audio: bool) -> AudioSegment | None:
    """clip 生成后登记/更新 H3 原生音频片段（executor 调用）。

    - 视频重新生成后重新检测：删除该镜头旧 native_audio 片段，按检测结果重建
    - 无音频流：登记 absent 状态片段（generation_status=completed, note=absent），流程不阻断
    """
    old = db.execute(
        select(AudioSegment).where(
            AudioSegment.shot_id == shot_id,
            AudioSegment.track_type == "native_audio",
        )
    ).scalars().all()
    for seg in old:
        db.delete(seg)
    seg = AudioSegment(
        id=ids.audio_segment_id(),
        project_id=project_id, episode=episode, shot_id=shot_id,
        track_type="native_audio", segment_type="h3_native",
        video_asset_id=video_asset_id,
        generation_status="completed",
        review_status="pending",
        version=1,
        note="absent" if not has_audio else None,
    )
    db.add(seg)
    db.commit()
    return seg
