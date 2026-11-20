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
from app.integrations.llm_response import (
    ERR_SCHEMA_MISMATCH,
    validate_parse_payload,
)
from app.integrations.llm_retry import call_with_retry
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
from app.services.script_chunker import (
    EpisodeChunk,
    fallback_split,
    slice_range,
    split_episodes,
)


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
    """按集 upsert 集级解析记录（复用脚本解析服务同款状态机语义）。"""
    from app.services.script_parse import upsert_episode_records
    upsert_episode_records(db, project_id, episode_start, episode_count, status,
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


def _build_global_context(preamble: str, char_map: dict, outline_episodes: dict,
                          b_start: int, b_end: int, *, limit: int = 800) -> str:
    """拼「全剧上下文」块（≤ limit 字）：已知角色名 + 本批集大纲标题 + 人物表节选。

    并行批次互相看不到对方正文，角色/场景命名容易漂移；这里只喂已入库角色名
    与大纲标题做缓解，不引入批次间串行依赖（跨批去重仍走 normalize_entity_name）。
    """
    parts: list[str] = []
    names = [n for n in (char_map or {}) if n]
    if names:
        parts.append("全剧上下文——已知角色：" + "、".join(names[:40]) + "\n")
    titles = []
    for ep in range(b_start, b_end + 1):
        info = (outline_episodes or {}).get(ep) or {}
        title = str(info.get("title") or "").strip()
        if title:
            titles.append(f"第{ep}集《{title}》")
    if titles:
        parts.append("本批集大纲标题：" + "；".join(titles) + "\n")
    used = sum(len(p) for p in parts)
    room = max(0, limit - used)
    head = (preamble or "").strip()
    if head and room > 0:
        parts.append("人物表/序言节选：\n" + head[:room] + "\n")
    return "".join(parts)


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
            if not params.get("retry") and _pp.get("script_version_id") == sv.id and int(_pp.get("episode_start") or 1) == episode_start:
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

        # ---- 4. 批次划分：口径已由大纲前置锁定 ----
        # agent_first_batch_auto=True：首次任务仅解析首批，后续「继续分析」逐批推进
        # agent_first_batch_auto=False：单次任务解析全部批次（旧行为）
        first_batch_auto = getattr(settings, "agent_first_batch_auto", True)
        # 先按全量划分批次
        batches = []
        s = episode_start
        while s <= total_episodes:
            e = min(s + max_per_batch - 1, total_episodes)
            batches.append((s, e))
            s = e + 1
        if first_batch_auto:
            # True：首次只解析首批，前端「继续分析」触发后续批次
            batches = batches[:1]
        batch_end = batches[-1][1]
        episode_count = batch_end - episode_start + 1

        # ---- 5. 批次并行 LLM 解析（每批独立调用；delta 事件经队列串行落库）----
        from app.models import Character
        char_map = {
            c.name: c.id
            for c in db.query(Character).filter(Character.project_id == job.project_id).all()
        }

        # ---- 4.5 按集切片：每批只发送本批集正文 ----
        # 旧实现 `hasattr(sv, "get_episode_chunk")` 恒为 False（ScriptVersion 上
        # 并无该方法），于是每一批都发送整本剧本。这里改为显式切分。
        split = split_episodes(text)
        preamble = ""
        chunk_mode = "markers"
        if split is not None:
            chunks: list[EpisodeChunk] = list(split.chunks)
            preamble = split.preamble
            if len(chunks) != total_episodes:
                # 标记数与大纲口径不一致：不让任务失败，只留痕
                _write_event(db, job, "agent.chunk_mismatch", {
                    "marker_count": len(chunks),
                    "total_episodes": total_episodes,
                })
            if len(chunks) < total_episodes:
                # 缺失集走近似均分补齐，避免切片后漏集
                have = {c.number for c in chunks}
                chunks += [c for c in fallback_split(text, total_episodes) if c.number not in have]
                chunk_mode = "markers+fallback"
        else:
            # 全无集标记：按场次/段落边界近似均分
            chunks = fallback_split(text, total_episodes)
            chunk_mode = "fallback"
        outline_episodes = {
            int(e.get("episode") or 0): e
            for e in (outline_data.get("episode_breakdown") or [])
        }
        _write_event(db, job, "agent.chunking", {
            "mode": chunk_mode,
            "chunk_count": len(chunks),
            "preamble_chars": len(preamble or ""),
            "batch_chars": [
                {"range": f"{bs}-{be}", "chars": len(slice_range(chunks, bs, be))}
                for bs, be in batches
            ],
        })
        db.commit()
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
            """单批次 LLM 解析（工作线程内执行，不触碰数据库）。

            重试策略统一由 ``llm_retry.call_with_retry`` 承担：按错误码决定
            放大 token / 修复提示 / 退避，不再一律当截断处理。
            每次尝试的元数据经 ``attempts`` 返回，供主线程写事件。
            """
            b_count = b_end - b_start + 1

            # 只发送本批集正文；切片缺失时退回全文，避免发出空正文让模型凭空编造
            episode_text = slice_range(chunks, b_start, b_end) or text
            user_prompt = (
                style_config.parse_context_block(style_settings)
                + _build_global_context(preamble, char_map, outline_episodes, b_start, b_end)
                + f"请解析以下剧本，仅解析第 {b_start} 集到第 {b_end} 集，"
                + f"共 {b_count} 集。范围外的集不要输出。\n\n"
                + f"第 {b_start}–{b_end} 集剧本：\n{episode_text}"
            )

            def _on_delta(delta_text: str) -> None:
                delta_q.put(delta_text)

            attempts: list[dict] = []

            def _record(rec) -> None:
                attempts.append(rec.to_dict())

            def _parse_and_validate(text: str) -> dict:
                """JSON 解析 + 业务校验都放在重试循环内部。

                只做 JSON 解析是不够的：模型可以吐出完全合法的 JSON，
                但每个 scene 都没有 shots（真实发生：场景 7 个 / 分镜 0 个）。
                这种情况必须在**重试循环内**判失败，才能追加 schema 约束提示
                再问一次；放到循环外就变成了「解析成功但结果不可用」，
                最后仍然落库，违反「不完整输出不能保存」。
                """
                data = parse_llm_json(text)
                problems = validate_parse_payload(data)
                if problems:
                    raise LLMError(
                        ERR_SCHEMA_MISMATCH,
                        "解析结果业务校验未通过: " + "；".join(problems[:8]),
                    )
                return data

            raw = call_with_retry(
                client,
                system_prompt=get_prompt("parse_system"),
                user_prompt=user_prompt,
                json_mode=True,
                stream=True,
                on_delta=_on_delta,
                on_attempt=_record,
                validate=_parse_and_validate,
            )
            return {"start": b_start, "end": b_end, "raw": raw, "attempts": attempts}

        writer_thread = threading.Thread(target=_delta_writer, daemon=True)
        writer_thread.start()
        batch_results: list[dict] = []
        batch_errors: list[tuple[tuple[int, int], LLMError]] = []
        max_workers = max(1, min(len(batches), int(getattr(settings, "llm_parse_concurrency", 3) or 3)))
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
            # 单集独立失败：成功集保留，仅标记失败集为 failed
            for (bs, be), e in batch_errors:
                _upsert_records(db, job.project_id, bs, be - bs + 1, "failed",
                                error_msg=e.message[:2000], parse_version_id=pv.id)
            failed_desc = "; ".join(
                f"第 {bs}-{be} 集: {e.message[:200]}" for (bs, be), e in batch_errors[:3]
            )
            # 仅所有批次都失败时才标记整体失败；部分成功则标记 partial
            if not batch_results:
                _fail_job(db, job, batch_errors[0][1].code, f"所有批次解析失败: {failed_desc}")
                return {"ok": False, "reason": batch_errors[0][1].code}
            # 部分失败：写入 succeeded 前先更新失败集状态
            db.commit()

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
            # 记录每次尝试的元数据（token 预算 / finish_reason / 用量），
            # 用于复盘「是否真的放大了预算」「空输出时上游到底返回了什么」
            if br.get("attempts"):
                _write_event(db, job, "agent.parse_attempts", {
                    "range": f"{br['start']}-{br['end']}",
                    "attempts": br["attempts"],
                })
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

        # 落库前最后一道业务门禁：重试循环内的 validate 只校验「单批」结果，
        # 这里校验**合并后**的最终结果。宁可整任务失败，也不能把
        # 「场景 7 个 / 分镜 0 个」这种不可用结果写进 ParseVersion 与 job.result。
        final_problems = validate_parse_payload(merged)
        if final_problems:
            msg = "解析结果业务校验未通过，已拒绝落库: " + "；".join(final_problems[:8])
            # 保留原始响应供定位（模型漏输出 / 字段名不符 / 静默截断）
            job.result = {
                "rejected": True,
                "problems": final_problems,
                "raw_batches": [
                    {"range": f"{br['start']}-{br['end']}", "raw": br["raw"]}
                    for br in batch_results
                ],
            }
            _upsert_records(db, job.project_id, episode_start, episode_count, "failed",
                            error_msg=msg, parse_version_id=pv.id)
            _fail_job(db, job, ERR_SCHEMA_MISMATCH, msg)
            return {"ok": False, "reason": ERR_SCHEMA_MISMATCH, "problems": final_problems}

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
