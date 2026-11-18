"""剧本一体化分析 Agent（script-agent）：评估+解析合并，集数由 LLM 大纲分析决定。

流程（批次并行化改造）：
1. LLM 大纲分析：analyze_script_outline 输出分集结构（代码规则估算已移除，
   集数一律以 LLM 分析为准），口径与批次边界由此前置锁定
2. 软闸口评估：evaluate_script 生成评分与问题清单（失败不阻断，仅记录事件）
3. 批次并行解析：ThreadPoolExecutor 并行扇出各批次 LLM 调用（每批独立调用，
   互不依赖）；流式 delta 经内存队列由写入线程串行落库（SQLite 单写者）
4. 合并落库：各批归一化结果按实体名去重合并（角色/道具/场景跨批一致），
   主线程串行 upsert；任一批失败则整体失败并标注失败批次范围
5. agent_first_batch_auto=true 时全量并行；false 回退仅首批（旧行为，供对照）

纪律：与 parse_executor 一致——LLM 调用在事务外，事件与状态同事务写 outbox。
"""
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.integrations.llm import LLMClient, LLMError
from app.models import EpisodeParseRecord, Job, JobEvent, ParseVersion, ScriptVersion
from app.models import ids
from app.services.script_parse import (
    analyze_script_outline,
    normalize_entity_name,
    normalize_parse_output,
    normalize_scene_name,
)
from app.services.prompt_store import get_prompt
from app.services import style_config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq


def _fail_job(db: Session, job: Job, code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = code
    job.error_msg = message[:2000]
    job.finished_at = _now()
    _write_event(db, job, "job.failed", {"error_code": code, "error_msg": job.error_msg})
    # 终态清理 delta 事件：失败原因已存 error_code/error_msg，流式事件仅用于过程展示
    db.query(JobEvent).filter(
        JobEvent.job_id == job.id, JobEvent.event_type == "agent.llm_delta"
    ).delete(synchronize_session=False)
    db.commit()


def _upsert_records(db: Session, project_id: str, episode_start: int, episode_count: int,
                    status: str, *, error_msg: str | None = None,
                    asset_counts: dict[int, int] | None = None,
                    parse_version_id: str | None = None) -> None:
    """按集 upsert 集级解析记录（复用 parse_executor 同款状态机语义）。"""
    from app.worker.parse_executor import _upsert_records as _upsert
    _upsert(db, project_id, episode_start, episode_count, status,
            error_msg=error_msg, asset_counts=asset_counts,
            parse_version_id=parse_version_id)


def _find_or_create_parse_version(db: Session, project_id: str, script_version_id: str,
                                  script_hash: str) -> ParseVersion:
    """按 project + script_hash 匹配已有解析版本（candidate 或 current，多批次共用），无则创建。

    兼容 current：批次 1 确认后（status=current）批次 2 继续分析时复用同一 ParseVersion，
    避免版本分裂与 diff_summary 丢失；剧本 hash 变化则不匹配（口径失效，创建新版本）。
    """
    pv = (
        db.query(ParseVersion)
        .filter(
            ParseVersion.project_id == project_id,
            ParseVersion.script_hash == script_hash,
            ParseVersion.status.in_(["candidate", "current"]),
        )
        .order_by(ParseVersion.version.desc())
        .first()
    )
    if pv is not None:
        return pv
    max_ver = (
        db.query(ParseVersion.version)
        .filter(ParseVersion.project_id == project_id)
        .order_by(ParseVersion.version.desc())
        .first()
    )
    next_ver = (max_ver[0] if max_ver else 0) + 1
    pv = ParseVersion(
        id=ids.parse_version_id(),
        project_id=project_id,
        script_version_id=script_version_id,
        version=next_ver,
        status="candidate",
        script_hash=script_hash,
        created_at=_now().replace(tzinfo=None),
    )
    db.add(pv)
    db.flush()
    return pv


def execute_script_agent_job(job_id: str) -> dict:
    """执行一个 script-agent 一体化分析任务（批次并行解析）。返回执行结果摘要。"""
    from app.core.config import get_settings
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"ok": False, "reason": "job_not_found"}
        if job.status not in ("queued",):
            return {"ok": False, "reason": f"invalid_status:{job.status}"}

        job.status = "running"
        job.started_at = _now()
        job.attempts = (job.attempts or 0) + 1
        _write_event(db, job, "agent.started", {"attempt": job.attempts})
        db.commit()

        params = dict(job.params or {})
        script_version_id = params.get("script_version_id")
        episode_start = int(params.get("episode_start") or 1)
        sv = db.get(ScriptVersion, script_version_id) if script_version_id else None
        if sv is None:
            _fail_job(db, job, "script_version_not_found", "剧本版本不存在")
            return {"ok": False, "reason": "script_version_not_found"}
        text = sv.content_snapshot
        script_hash = sv.content_hash or ""

        # ---- 0. 幂等防御：同剧本版本且同 episode_start 已有成功任务时，
        # 复用其结果直接标记 succeeded，跳过 LLM 大纲/评估与解析 ----
        _done = (
            db.query(Job)
            .filter(
                Job.project_id == job.project_id,
                Job.job_type == "script_agent",
                Job.status == "succeeded",
                Job.id != job.id,
            )
            .order_by(Job.created_at.desc())
            .all()
        )
        for _prev in _done:
            _pp = _prev.params or {}
            if _pp.get("script_version_id") == sv.id and int(_pp.get("episode_start") or 1) == episode_start:
                job.status = "succeeded"
                job.result = dict(_prev.result or {})
                job.result["idempotent_reuse"] = True
                job.result["reused_from_job_id"] = _prev.id
                job.finished_at = _now()
                _write_event(db, job, "agent.succeeded", {
                    "idempotent_reuse": True,
                    "reused_from_job_id": _prev.id,
                })
                db.commit()
                return {"ok": True, "idempotent_reuse": True, "reused_from_job_id": _prev.id}

        # ---- 1. LLM 大纲分析（代码规则估算已移除，集数一律以 LLM 分析为准）----
        from app.models import Project
        project = db.get(Project, job.project_id)
        settings_map = (project.settings or {}) if project else {}
        episode_duration_target = int(settings_map.get("episode_duration_target", 90))
        max_per_batch = int(settings_map.get("max_episodes_per_batch") or get_settings().max_episodes_per_batch)
        try:
            outline_data = analyze_script_outline(
                text,
                episode_duration_target=episode_duration_target,
                max_episodes_per_batch=max_per_batch,
            )
        except LLMError as e:
            _fail_job(db, job, e.code, f"大纲分析失败: {e.message}")
            return {"ok": False, "reason": e.code}
        _write_event(db, job, "agent.precheck", {
            "estimated_episodes": outline_data.get("estimated_episodes"),
            "estimated_total_duration": outline_data.get("estimated_total_duration"),
            "exceeds_batch_limit": outline_data.get("exceeds_batch_limit"),
        })
        db.commit()

        # ---- 2. 软闸口评估（失败不阻断）：流式输出评估过程 ----
        evaluation = None
        try:
            from app.services.script_evaluation import evaluate_script

            def _on_eval_delta(delta_text: str) -> None:
                _write_event(db, job, "agent.llm_delta", {"delta": delta_text, "phase": "evaluation"})
                db.commit()

            evaluation = evaluate_script(text, outline_data, settings_map, on_delta=_on_eval_delta)
            _write_event(db, job, "agent.eval_info", {
                "score": evaluation.get("score"),
                "issues_count": len(evaluation.get("issues") or []),
            })
            db.commit()
        except Exception as e:  # noqa: BLE001
            _write_event(db, job, "agent.eval_skipped", {"reason": str(e)[:200]})
            db.commit()

        # ---- 3. 口径确定：优先已锁定口径，否则用 LLM 大纲集数 ----
        pv = _find_or_create_parse_version(db, job.project_id, sv.id, script_hash)
        total_episodes = pv.total_episodes or int(outline_data.get("estimated_episodes") or 1)
        total_episodes = max(1, total_episodes)
        if episode_start > total_episodes:
            _fail_job(db, job, "invalid_request", f"episode_start {episode_start} 超出口径总集数 {total_episodes}")
            return {"ok": False, "reason": "out_of_scope"}

        # ---- 4. 批次划分：口径已由大纲前置锁定，全量并行 ----
        first_batch_auto = getattr(settings, "agent_first_batch_auto", True)
        if first_batch_auto:
            batches = []
            s = episode_start
            while s <= total_episodes:
                e = min(s + max_per_batch - 1, total_episodes)
                batches.append((s, e))
                s = e + 1
        else:
            # 回退口径：仅解析首批（旧行为，供对照验证）
            batches = [(episode_start, min(episode_start + max_per_batch - 1, total_episodes))]
        batch_end = batches[-1][1]
        episode_count = batch_end - episode_start + 1

        # ---- 5. 批次并行 LLM 解析（每批独立调用；delta 事件经队列串行落库）----
        from app.models import Character
        char_map = {
            c.name: c.id
            for c in db.query(Character).filter(Character.project_id == job.project_id).all()
        }
        client = LLMClient()
        style_settings = (project.settings if project is not None else {}) or {}
        style_snap = style_config.style_snapshot(style_settings, project_id=job.project_id)
        _upsert_records(db, job.project_id, episode_start, episode_count, "running", parse_version_id=pv.id)
        db.commit()

        from app.integrations.llm import parse_llm_json
        delta_q: "queue.Queue[str]" = queue.Queue()
        _writer_stop = threading.Event()

        def _delta_writer() -> None:
            """串行事件写入线程：SQLite 单写者；多批 delta 聚合节流（≥1s 或 ≥240 字符）后落库。"""
            import time as _time
            wdb: Session = SessionLocal()
            buf: list[str] = []
            last_flush = _time.monotonic()
            try:
                while True:
                    stopped = _writer_stop.is_set()
                    while True:
                        try:
                            d = delta_q.get_nowait()
                        except queue.Empty:
                            break
                        buf.append(d)
                    now = _time.monotonic()
                    if buf and (now - last_flush >= 1.0 or sum(len(x) for x in buf) >= 240):
                        delta = "".join(buf)
                        buf.clear()
                        last_flush = now
                        try:
                            wj = wdb.get(Job, job_id)
                            if wj is not None:
                                _write_event(wdb, wj, "agent.llm_delta", {"delta": delta})
                                wdb.commit()
                        except Exception:
                            wdb.rollback()
                    if stopped and not buf:
                        break
                    _time.sleep(0.2)
            finally:
                wdb.close()

        def _parse_batch(b_start: int, b_end: int) -> dict:
            """单批次 LLM 解析（工作线程内执行，不触碰数据库）。"""
            b_count = b_end - b_start + 1
            user_prompt = (
                style_config.parse_context_block(style_settings)
                + f"请解析以下剧本，仅解析第 {b_start} 集到第 {b_end} 集，"
                + f"共 {b_count} 集。范围外的集不要输出。\n\n剧本全文：\n{text}"
            )

            def _on_delta(delta_text: str) -> None:
                delta_q.put(delta_text)

            content = client.chat_stream(
                get_prompt("parse_system"), user_prompt, json_mode=True, on_delta=_on_delta,
            )
            raw = parse_llm_json(content)
            return {"start": b_start, "end": b_end, "raw": raw}

        writer_thread = threading.Thread(target=_delta_writer, daemon=True)
        writer_thread.start()
        batch_results: list[dict] = []
        batch_errors: list[tuple[tuple[int, int], LLMError]] = []
        max_workers = max(1, min(len(batches), 4))
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dlab-parse") as pool:
                fut_map = {pool.submit(_parse_batch, bs, be): (bs, be) for bs, be in batches}
                for fut in as_completed(fut_map):
                    rng = fut_map[fut]
                    try:
                        batch_results.append(fut.result())
                    except LLMError as e:
                        batch_errors.append((rng, e))
        finally:
            _writer_stop.set()
            writer_thread.join(timeout=5)

        if batch_errors:
            failed_desc = "; ".join(
                f"第 {bs}-{be} 集: {e.message[:200]}" for (bs, be), e in batch_errors[:3]
            )
            _upsert_records(db, job.project_id, episode_start, episode_count, "failed",
                            error_msg=failed_desc[:2000], parse_version_id=pv.id)
            _fail_job(db, job, batch_errors[0][1].code, f"批次解析失败: {failed_desc}")
            return {"ok": False, "reason": batch_errors[0][1].code}

        # ---- 6. 合并各批归一化结果（主线程串行；跨批实体去重）----
        merged: dict = {
            "project_analysis": {}, "characters": [], "props": [],
            "scenes": [], "episodes": [],
        }
        parse_warnings: list = []
        review_issues: list = []
        seen_chars: set = set()
        seen_props: set = set()
        seen_scenes: set = set()
        batch_results.sort(key=lambda r: r["start"])
        for br in batch_results:
            norm = normalize_parse_output(
                br["raw"], episode_start=br["start"],
                episode_count=br["end"] - br["start"] + 1,
                character_name_to_id=char_map,
            )
            data = norm.data
            if not merged["project_analysis"]:
                merged["project_analysis"] = data.get("project_analysis") or {}
            for c in data.get("characters") or []:
                key = normalize_entity_name(c.get("name") or "")
                if key and key not in seen_chars:
                    seen_chars.add(key)
                    merged["characters"].append(c)
            for p in data.get("props") or []:
                key = normalize_entity_name(p.get("name") or "")
                if key and key not in seen_props:
                    seen_props.add(key)
                    merged["props"].append(p)
            for sc in data.get("scenes") or []:
                key = normalize_scene_name(sc.get("title") or "", sc.get("time_note"))
                if not key:
                    merged["scenes"].append(sc)
                    continue
                if key in seen_scenes:
                    # 跨批同名场景（同一地点）：合并 shots，避免丢镜头
                    for msc in merged["scenes"]:
                        if normalize_scene_name(msc.get("title") or "", msc.get("time_note")) == key:
                            msc["shots"] = (msc.get("shots") or []) + (sc.get("shots") or [])
                            break
                else:
                    seen_scenes.add(key)
                    merged["scenes"].append(sc)
            merged["episodes"].extend(data.get("episodes") or [])
            parse_warnings.extend(norm.parse_warnings)
            review_issues.extend(norm.review_issues)
        merged["episodes"].sort(key=lambda e: int(e.get("episode") or 0))

        # ---- 7. 口径锁定与落库 ----
        if pv.total_episodes is None:
            pv.total_episodes = total_episodes
        pv.script_hash = script_hash

        shot_total = sum(len(sc.get("shots") or []) for sc in merged.get("scenes") or [])
        per_episode_shots: dict[int, int] = {}
        for sc in merged.get("scenes") or []:
            for sh in sc.get("shots") or []:
                ep = int(sh.get("episode") or episode_start)
                per_episode_shots[ep] = per_episode_shots.get(ep, 0) + 1
        _upsert_records(db, job.project_id, episode_start, episode_count, "succeeded",
                        asset_counts=per_episode_shots, parse_version_id=pv.id)

        # diff_summary 增量合并（多批次共用同一 ParseVersion）
        prev_summary = pv.diff_summary or {}
        pv.diff_summary = {
            "characters": len(merged.get("characters") or []),
            "props": len(merged.get("props") or []),
            "scenes": len(merged.get("scenes") or []),
            "shots": (prev_summary.get("shots") or 0) + shot_total,
            "episodes": total_episodes,
            "parse_warnings": len(parse_warnings),
            "review_issues": len(review_issues),
        }

        # ---- 8. 终态 ----
        has_more = batch_end < total_episodes
        job.status = "succeeded"
        style_diffs = style_config.compute_style_diff(
            style_settings, merged.get("project_analysis"))
        job.result = {
            **merged,
            "style_config_snapshot": style_snap,
            "style_config_diffs": style_diffs,
            "parse_warnings": parse_warnings,
            "review_issues": review_issues,
            "episode_start": episode_start,
            "episode_count": episode_count,
            "total_episodes": total_episodes,
            "has_more": has_more,
            "next_episode_start": batch_end + 1 if has_more else None,
            "evaluation": evaluation,
            "parse_version_id": pv.id,
        }
        job.finished_at = _now()
        _write_event(db, job, "agent.succeeded", {
            "parse_version_id": pv.id,
            "episodes": f"{episode_start}-{batch_end}/{total_episodes}",
            "shots": shot_total,
            "has_more": has_more,
            "batches": len(batches),
        })
        # 终态清理 delta 事件：完整结果已存 job.result，流式事件仅用于过程展示
        db.query(JobEvent).filter(
            JobEvent.job_id == job.id, JobEvent.event_type == "agent.llm_delta"
        ).delete(synchronize_session=False)
        db.commit()
        return {"ok": True, "parse_version_id": pv.id, "shots": shot_total, "has_more": has_more}
    except Exception as e:  # noqa: BLE001
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None and job.status not in ("succeeded", "failed"):
                _fail_job(db, job, "llm_error", f"Agent 执行器异常: {e}")
        except Exception:
            pass
        return {"ok": False, "reason": f"executor_exception:{e}"}
    finally:
        db.close()
