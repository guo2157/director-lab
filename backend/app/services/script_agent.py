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
    ERR_OUTPUT_TRUNCATED,
    ERR_SCHEMA_MISMATCH,
    validate_parse_payload,
)
from app.integrations.llm_retry import call_with_retry
from app.integrations.token_estimate import precheck_input_budget
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
from app.services import job_cancel
from app.services.script_chunker import (
    EpisodeChunk,
    fallback_split,
    slice_range,
    split_episodes,
    split_text_by_blocks,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_event(db: Session, job: Job, event_type: str, payload: dict | None = None) -> None:
    ev = JobEvent(job_id=job.id, event_type=event_type, payload=payload or {}, created_at=_now())
    db.add(ev)
    db.flush()
    job.event_seq = ev.event_seq
    # 统一任务编排层桥接：job_event → task_event（Job 归属任务时同步任务阶段状态）
    # 与 parse_executor 保持一致；缺失会让 parse 阶段永远停在 pending
    try:
        from app.services.task_orchestrator import bridge_job_event
        bridge_job_event(db, job_id=job.id, event_type=event_type, payload=payload or {})
    except Exception:  # noqa: BLE001
        pass  # 桥接失败不影响既有执行链路


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
            # params.force 来自「重新解析」按钮：必须真正重跑，不能复用旧结果。
            # 只判断 params.retry 是不够的 —— 那条路径是任务失败后重试，
            # 用户点「重新解析」时上一个任务往往是 succeeded 的。
            if (not params.get("retry") and not params.get("force")
                    and _pp.get("script_version_id") == sv.id
                    and int(_pp.get("episode_start") or 1) == episode_start):
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

        # ---- 0.9 输入预算预检 ----
        # 超长输入必须在发起 LLM 之前明确失败：否则供应商返回「上下文超限」
        # 会被当成可退避重试的供应商错误，白白重试 N 次后仍然失败。
        # allow_segmented=True：整本超预算但单集放得下时，走分段大纲而不是失败。
        precheck = precheck_input_budget(text, settings=settings, allow_segmented=True)
        # 取消检查点：LLM 调用是同步阻塞的（单次实测可达 24 分钟），
        # 数据库里的 status 改成 cancelled 线程是感知不到的。所以取消走
        # 进程内信号，并在每个可能长时间停留的位置轮询。
        def _check_cancel(stage: str) -> None:
            if job_cancel.is_cancelled(job_id):
                _write_event(db, job, "agent.cancelled", {"stage": stage})
                db.commit()
                raise job_cancel.JobCancelled(job_id)

        def _is_cancelled() -> bool:
            return job_cancel.is_cancelled(job_id)

        _write_event(db, job, "agent.input_precheck", {
            "ok": precheck["ok"],
            "code": precheck["code"],
            "estimated_tokens": precheck["estimated_tokens"],
            "budget": precheck["budget"],
            "chars": precheck["chars"],
            "episode": precheck["episode"],
            "outline_mode": precheck["outline_mode"],
        })
        if not precheck["ok"]:
            _fail_job(db, job, precheck["code"], precheck["message"])
            return {"ok": False, "reason": precheck["code"]}
        db.commit()

        # ---- 0.95 模型输出能力校验（旁路，不阻断）----
        # 实测事故（2026-11-21）：配置 llm_parse_max_tokens=65536 但模型上限
        # 只有 16000，网关静默截断且 finish_reason 谎报 stop，导致「翻倍重试」
        # 永远空转，每个任务白烧 80–90 秒。
        # 这里只告警不拦截：查不到能力（老网关无该字段）也必须放行，
        # 否则会把正常任务误杀。告警写进 agent.model_capability，前端可提示。
        from app.integrations.model_capability import check_and_record

        check_and_record(
            settings,
            write_event=lambda et, payload: _write_event(db, job, et, payload),
        )
        db.commit()

        # 阶段事件：解析前 40 秒只有空输出框，用户无法判断是否在跑。
        # 这些阶段由后端真实上报（不是前端按 JSON 猜），前端只负责展示。
        _write_event(db, job, "agent.stage", {
            "stage": "outline",
            "label": "正在分析剧本结构、估算集数",
            "chars": len(text),
        })
        db.commit()
        _check_cancel("outline")
        try:
            outline_data = analyze_script_outline(
                text,
                episode_duration_target=episode_duration_target,
                max_episodes_per_batch=max_per_batch,
                segmented=(precheck["outline_mode"] == "segmented"),
                settings=settings,
            )
        except LLMError as e:
            _fail_job(db, job, e.code, f"大纲分析失败: {e.message}")
            return {"ok": False, "reason": e.code}
        _write_event(db, job, "agent.precheck", {
            "estimated_episodes": outline_data.get("estimated_episodes"),
            "estimated_total_duration": outline_data.get("estimated_total_duration"),
            "exceeds_batch_limit": outline_data.get("exceeds_batch_limit"),
            "outline_mode": precheck["outline_mode"],
        })
        db.commit()
        _check_cancel("outline_done")

        # ---- 2. 软闸口评估（失败不阻断）：流式输出评估过程 ----
        # 默认关闭（script_evaluation_enabled=False）：实测单集任务里该阶段固定耗时 84s
        # 且每次都因字段校验不通过而 eval_skipped，零产出却占整个任务 32% 的时间。
        # 解析页的职责是拆解剧本供下游制作，评估报告没有下游消费方。
        evaluation = None
        _eval_on = bool(getattr(settings, "script_evaluation_enabled", False))
        if not _eval_on:
            _write_event(db, job, "agent.eval_skipped", {
                "reason": "script_evaluation_enabled=false（评估阶段默认关闭）",
            })
            db.commit()
        else:
            try:
                from app.services.script_evaluation import evaluate_script

                _write_event(db, job, "agent.stage", {
                    "stage": "evaluation",
                    "label": "正在评估剧本质量（评分与问题清单）",
                    "estimated_episodes": outline_data.get("estimated_episodes"),
                })
                db.commit()

                def _on_eval_delta(delta_text: str) -> None:
                    # 评估阶段同样是长流式调用：用户点停止后必须尽快退出，
                    # 否则会继续把评估跑完才响应取消。
                    if _is_cancelled():
                        raise job_cancel.JobCancelled(job_id)
                    _write_event(db, job, "agent.llm_delta", {"delta": delta_text, "phase": "evaluation"})
                    db.commit()

                evaluation = evaluate_script(
                    text, outline_data, settings_map,
                    on_delta=_on_eval_delta, settings=settings,
                    should_cancel=_is_cancelled,
                    cancel_token=job_id,
                )
                _write_event(db, job, "agent.eval_info", {
                    "overall_score": evaluation.get("overall_score"),
                    "issues_count": len(evaluation.get("issues") or []),
                })
                db.commit()
            except job_cancel.JobCancelled:
                # 取消不能被「评估失败不阻断」的兜底吞掉，否则任务会继续往下解析。
                raise
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
        missing_episodes: list[int] = []
        if split is not None:
            chunks: list[EpisodeChunk] = list(split.chunks)
            preamble = split.preamble
            if len(chunks) != total_episodes:
                # 标记数与大纲口径不一致：不让任务失败，只留痕
                _write_event(db, job, "agent.chunk_mismatch", {
                    "marker_count": len(chunks),
                    "total_episodes": total_episodes,
                })
            # 只认标记切出的真实集。缺失的集号**不得用 fallback_split 补齐**：
            #
            # 旧实现把 ``fallback_split(text, total_episodes)`` 里集号没出现的片段
            # 追加进来，但 fallback 是按全文均分出的「近似段」，与标记集号毫无对应
            # 关系。实测（backend/_probe_truth.py，2026-11-21）：
            #   剧本只有第1/2集标记、大纲 4 集时，补齐进来的「第3集」实际装的是
            #   **第2集的正文**（fallback 把全文切成 4 份，第 3 份正好落在第2集上）。
            # 后果是第2集剧情被当成第3集重复解析、落库成重复镜头，比漏集更难查。
            # 正确做法：缺失集号如实记录，由 _parse_batch 让该批明确失败。
            have = {c.number for c in chunks}
            missing_episodes = [n for n in range(1, total_episodes + 1) if n not in have]
            if missing_episodes:
                _write_event(db, job, "agent.chunk_missing_episodes", {
                    "missing": missing_episodes,
                    "marker_count": len(chunks),
                    "total_episodes": total_episodes,
                })
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
            # 缺失集号：前端据此提示「第 N 集未在剧本中找到集标记」，
            # 而不是等解析失败后只看到一句「切片为空」。
            "missing_episodes": missing_episodes,
            "batch_chars": [
                {"range": f"{bs}-{be}", "chars": len(slice_range(chunks, bs, be))}
                for bs, be in batches
            ],
        })
        _write_event(db, job, "agent.stage", {
            "stage": "parsing",
            "label": f"正在拆解第 {episode_start}–{episode_start + episode_count - 1} 集（共 {len(batches)} 批）",
            "batches": [f"{bs}-{be}" for bs, be in batches],
        })
        db.commit()
        # 关键检查点：进入 LLM 解析前必须再确认一次取消。
        # 少了这一道，用户在评估阶段点停止后仍会白启一批 LLM 调用
        # （实测取消后多跑了 chunking + stage + 一轮解析请求才退出）。
        _check_cancel("before_parsing")
        client = LLMClient()
        style_settings = (project.settings if project is not None else {}) or {}
        style_snap = style_config.style_snapshot(style_settings, project_id=job.project_id)
        _upsert_records(db, job.project_id, episode_start, episode_count, "running", parse_version_id=pv.id)
        db.commit()

        from app.integrations.llm import parse_llm_json, response_meta_for
        delta_q: "queue.Queue[str]" = queue.Queue()
        _writer_stop = threading.Event()
        # 进度计数：由 _on_delta 累加（纯内存、无锁竞争），_delta_writer 统一写库。
        # 用列表而不是 int，是为了在闭包里原地修改而不触发 nonlocal。
        _chars_seen = [0]

        def _bump_progress(n: int) -> None:
            """记录已接收字符数。真正的落库交给 _delta_writer（SQLite 单写者）。"""
            _chars_seen[0] += n

        def _delta_writer() -> None:
            """串行事件写入线程：SQLite 单写者；多批 delta 聚合节流（≥1s 或 ≥240 字符）后落库。"""
            import time as _time
            wdb: Session = SessionLocal()
            buf: list[str] = []
            last_flush = _time.monotonic()
            last_progress = _time.monotonic()
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
                                # 进度事件与 delta 同事务落库：前端据此显示真实推进，
                                # 而不是只有「已耗时」在涨、看不出有没有往前走。
                                if now - last_progress >= 2.0:
                                    last_progress = now
                                    _write_event(wdb, wj, "job.progress", {
                                        "value": _chars_seen[0],
                                        "max": 0,
                                        "chars": _chars_seen[0],
                                    })
                                wdb.commit()
                        except Exception:
                            wdb.rollback()
                    if stopped and not buf:
                        break
                    _time.sleep(0.2)
            finally:
                wdb.close()

        # 输出侧分段预算：0/负数表示关闭（回到整批一次调用）
        _seg_budget = int(getattr(settings, "llm_parse_segment_chars", 0) or 0)
        # 失败批次的尝试记录也必须留下：此前只有成功批次才写 agent.parse_attempts，
        # 一旦整批失败，真实 max_tokens / finish_reason / usage 全部丢失，
        # 复盘时只剩一句「max_tokens=0」无从定位。
        attempts_by_range: dict[str, list[dict]] = {}
        _LIST_KEYS = ("characters", "props", "prop_events", "scenes", "episodes")

        def _merge_payloads(payloads: list[dict]) -> dict:
            """把同一批多个分段（或递归二分）的解析结果按段顺序合并成一份。

            合并必须去重：多段各报一次同名角色会让 ``validate_parse_payload``
            以「角色名重复」直接判失败。这不是模型的问题，是分段带来的重复。
            去重口径与下游 ``normalize_parse_output`` 跨批合并保持一致：
            - 角色 / 道具：按 ``normalize_entity_name`` 归一化后的名字；
            - 场景：按 ``normalize_scene_name(title, time_note)``，同名场景
              **拼接 shots**（同一场戏被段边界切开时不能丢镜头）；
            - 集：按集号，取首个出现的；
            - 道具事件：按（道具, 集, 场景, 镜号, 类型）唯一。
            """
            merged_payload: dict = {"project_analysis": {}}
            for key in _LIST_KEYS:
                merged_payload[key] = []
            seen_char: dict[str, dict] = {}
            seen_prop: dict[str, dict] = {}
            scene_index: dict[str, dict] = {}
            seen_ep: set[int] = set()
            seen_prop_ev: set[tuple] = set()
            for p in payloads or []:
                if not isinstance(p, dict):
                    continue
                if not merged_payload["project_analysis"]:
                    merged_payload["project_analysis"] = p.get("project_analysis") or {}
                for c in p.get("characters") or []:
                    if not isinstance(c, dict):
                        continue
                    name = (c.get("name") or "").strip()
                    if not name:
                        continue
                    key = normalize_entity_name(name) or name
                    cur = seen_char.get(key)
                    if cur is None:
                        cur = dict(c)
                        seen_char[key] = cur
                        merged_payload["characters"].append(cur)
                        continue
                    for f in ("description", "traits", "appearance"):
                        if not str(cur.get(f) or "").strip() and str(c.get(f) or "").strip():
                            cur[f] = c.get(f)
                for pr in p.get("props") or []:
                    if not isinstance(pr, dict):
                        continue
                    name = (pr.get("name") or "").strip()
                    if not name:
                        continue
                    key = normalize_entity_name(name) or name
                    if key not in seen_prop:
                        seen_prop[key] = dict(pr)
                        merged_payload["props"].append(seen_prop[key])
                for ev in p.get("prop_events") or []:
                    if not isinstance(ev, dict):
                        continue
                    ev_key = (
                        (ev.get("prop") or "").strip(), ev.get("episode"),
                        (ev.get("scene_title") or "").strip(),
                        ev.get("shot_seq"), (ev.get("event_type") or "").strip(),
                    )
                    if ev_key in seen_prop_ev:
                        continue
                    seen_prop_ev.add(ev_key)
                    merged_payload["prop_events"].append(ev)
                for sc in p.get("scenes") or []:
                    if not isinstance(sc, dict):
                        continue
                    key = normalize_scene_name(
                        sc.get("title") or "", sc.get("time_note")) or ""
                    if not key:
                        merged_payload["scenes"].append(sc)
                        continue
                    cur = scene_index.get(key)
                    if cur is None:
                        scene_index[key] = sc
                        merged_payload["scenes"].append(sc)
                        continue
                    cur["shots"] = (cur.get("shots") or []) + (sc.get("shots") or [])
                    if not str(cur.get("description") or "").strip():
                        cur["description"] = sc.get("description")
                for ep in p.get("episodes") or []:
                    if not isinstance(ep, dict):
                        continue
                    try:
                        no = int(ep.get("episode") or 0)
                    except (TypeError, ValueError):
                        continue
                    if no in seen_ep:
                        continue
                    seen_ep.add(no)
                    merged_payload["episodes"].append(ep)
            return merged_payload

        def _segment_prompt(b_start: int, b_end: int, seg_text: str,
                            seg_idx: int, seg_total: int) -> str:
            b_count = b_end - b_start + 1
            head = (
                style_config.parse_context_block(style_settings)
                + _build_global_context(preamble, char_map, outline_episodes, b_start, b_end)
                + f"请解析以下剧本，仅解析第 {b_start} 集到第 {b_end} 集，"
                + f"共 {b_count} 集。范围外的集不要输出。\n"
            )
            if seg_total <= 1:
                # 不分段时与既有实现逐字符一致：prompt 措辞属于外部可见契约，
                # 改动会让「仅发送本集正文」的既有断言失效。
                return head + f"\n第 {b_start}–{b_end} 集剧本：\n{seg_text}"
            head += (
                f"本次只提供该范围内第 {seg_idx}/{seg_total} 段正文。"
                "只输出本段出现的场景与镜头；不要补写本段之外的剧情；"
                "本段首尾台词或动作可能不完整，仍要完整输出该场景。\n"
            )
            return head + f"\n第 {b_start}–{b_end} 集剧本（第 {seg_idx}/{seg_total} 段）：\n{seg_text}"

        def _run_segment(b_start: int, b_end: int, seg_text: str, seg_idx: int,
                         seg_total: int, depth: int, sink: list[dict], *,
                         strict: bool) -> dict:
            """解析一个正文分段；仍被判截断时对半再分（最多两层）。"""
            prompt = _segment_prompt(b_start, b_end, seg_text, seg_idx, seg_total)

            def _record(rec) -> None:
                rec_dict = rec.to_dict()
                rec_dict["segment"] = f"{seg_idx}/{seg_total}"
                rec_dict["depth"] = depth
                rec_dict["strict"] = strict
                sink.append(rec_dict)

            def _on_delta(delta_text: str) -> None:
                delta_q.put(delta_text)
                # 真实进度：按已接收字符数推进，而不是凭猜。
                # 没有它，长任务只能看到「已耗时」一直涨、没有任何推进感，
                # 用户无法判断是「在跑」还是「卡死」。
                _bump_progress(len(delta_text))

            def _validate(text: str) -> dict:
                """JSON 解析 + 业务校验都放在重试循环内部。

                只做 JSON 解析是不够的：模型可以吐出完全合法的 JSON，
                但每个 scene 都没有 shots（真实发生：场景 7 个 / 分镜 0 个）。
                这种情况必须在**重试循环内**判失败，才能追加 schema 约束提示
                再问一次；放到循环外就变成了「解析成功但结果不可用」，
                最后仍然落库，违反「不完整输出不能保存」。
                分段模式只要求本段有内容（整句校验在合并后统一做）。
                """
                # meta 必须带上本次调用的真实 finish_reason / usage / max_tokens：
                # 缺了它，截断错误会写成「max_tokens=0」，重试策略也失去放大依据。
                data = parse_llm_json(text, meta=response_meta_for(client, text))
                if strict:
                    problems = validate_parse_payload(data)
                    if problems:
                        raise LLMError(
                            ERR_SCHEMA_MISMATCH,
                            "解析结果业务校验未通过: " + "；".join(problems[:8]),
                        )
                    return data
                if not isinstance(data, dict):
                    raise LLMError(ERR_SCHEMA_MISMATCH, "该分段顶层不是 JSON 对象")
                if not (data.get("scenes") or data.get("episodes")):
                    raise LLMError(
                        ERR_SCHEMA_MISMATCH,
                        "该分段未解析出任何场景或集，需重试本段",
                    )
                return data

            try:
                return call_with_retry(
                    client,
                    system_prompt=get_prompt("parse_system"),
                    user_prompt=prompt,
                    json_mode=True,
                    stream=True,
                    on_delta=_on_delta,
                    on_attempt=_record,
                    validate=_validate,
                    should_cancel=_is_cancelled,
                    # 首轮就用解析专用预算（llm_parse_max_tokens，默认 65536），
                    # 不要从 llm_max_tokens（32768）起步。
                    #
                    # 原因（实测）：网关的 completion 上限是**间歇性**的——同一
                    # max_tokens 重复请求，有时 8109 正常收尾，有时硬停在 16000 且
                    # JSON 未闭合。从 32768 起步时首轮实测撞上限，白跑 77s，然后
                    # 重试放大到 65536 才成功（又 76s）。单集任务里这 77s 是纯浪费。
                    # 解析是已知的大输出场景，首轮直接给足预算，把「撞上限→重试」
                    # 这条路径从常态变成例外。
                    initial_max_tokens=int(
                        getattr(settings, "llm_parse_max_tokens", 65536) or 65536
                    ),
                    # cancel_token 让 chat_stream 在取消时直接 close 响应：
                    # should_cancel 只在下一条 SSE 行到达时才被检查，
                    # 供应商几十秒不吐字时线程会一直阻塞在 socket read 上。
                    cancel_token=job_id,
                )
            except LLMError as exc:
                # 取消不进入任何自救逻辑：既不能二分重试，也不能被当成截断。
                if exc.code == "llm_cancelled":
                    raise job_cancel.JobCancelled(job_id)
                # 供应商输出硬上限无法靠放大 max_tokens 突破：把这段正文对半再分，
                # 每半独立输出一份更小的 JSON。深度上限 2，避免无限递归。
                # 只要还能切出多于一段就二分：固定字符阈值会让「段本身不长、
                # 但输出仍顶到网关上限」的情况无法自救。depth 上限足以防止
                # 无限递归，切不动时 halves 只有一段，自然走下面 raise。
                if exc.code == ERR_OUTPUT_TRUNCATED and depth < 2:
                    halves = split_text_by_blocks(seg_text, max(1, len(seg_text) // 2))
                    if len(halves) > 1:
                        parts = [
                            _run_segment(b_start, b_end, half, seg_idx,
                                         seg_total, depth + 1, sink, strict=False)
                            for half in halves
                        ]
                        return _merge_payloads(parts)
                raise

        def _parse_batch(b_start: int, b_end: int) -> dict:
            """单批次 LLM 解析（工作线程内执行，不触碰数据库）。

            重试策略统一由 ``llm_retry.call_with_retry`` 承担：按错误码决定
            放大 token / 修复提示 / 退避，不再一律当截断处理。

            输出侧分段：部分网关的 completion 有硬上限（实测恒 16000 token 且
            finish_reason 谎报 stop），单次输出超长 JSON 必然中途截断。正文超过
            ``llm_parse_segment_chars`` 时按场次/段落边界分段解析，再合并为
            一份结果；合并后仍走完整的业务校验。
            """
            # 只发送本批集正文。切片为空时**不得退回全文**：
            #
            # 旧实现写的是 ``slice_range(...) or text``，注释理由是「避免发出空正文
            # 让模型凭空编造」。但实测（backend/_probe_slice.py，2026-11-21）证明
            # 这条兜底有两个真实触发条件，且后果比空正文严重得多：
            #
            #   1. 集标记不连续（第1集 / 第3集 / 第5集）：batch 2-2 取不到内容，
            #      整本剧本被发给单批，输入直接可能超上下文；
            #   2. 标记数少于大纲集数：``fallback_split`` 补齐的空片段（text=""），
            #      凡落在空片段上的批次全部回退全文。
            #
            # 长剧本下这等于把「按集切片」彻底失效——本该 2 千字的批发出几万字，
            # 既超预算又让模型把无关剧情写进本集。正确做法是让该批明确失败，
            # 由上层把失败区间写入 job.result，而不是静默发全文。
            episode_text = slice_range(chunks, b_start, b_end)
            if not (episode_text or "").strip():
                raise LLMError(
                    ERR_SCHEMA_MISMATCH,
                    f"第 {b_start}-{b_end} 集切片为空（集标记与大纲口径不一致），"
                    f"拒绝回退全文以免超出上下文预算",
                )
            segs = [episode_text]
            if _seg_budget > 0 and len(episode_text) > _seg_budget:
                segs = split_text_by_blocks(episode_text, _seg_budget)
            seg_total = len(segs)
            sink: list[dict] = []
            try:
                if seg_total <= 1:
                    raw = _run_segment(b_start, b_end, episode_text, 1, 1, 0, sink,
                                       strict=True)
                else:
                    # 各段 LLM 调用彼此独立（各段正文不重叠），在工作线程内
                    # 串行执行。batch 级已有 ThreadPoolExecutor 并行扇出，段内
                    # 再复用同一池做嵌套并行会导致死锁——所有 worker 都在等子任务。
                    parts = [
                        _run_segment(b_start, b_end, seg, idx + 1,
                                     seg_total, 0, sink, strict=False)
                        for idx, seg in enumerate(segs)
                    ]
                    raw = _merge_payloads(parts)
                    problems = validate_parse_payload(raw)
                    if problems:
                        raise LLMError(
                            ERR_SCHEMA_MISMATCH,
                            "分段合并后业务校验未通过: " + "；".join(problems[:8]),
                        )
                return {
                    "start": b_start, "end": b_end, "raw": raw,
                    "attempts": sink, "segments": seg_total,
                }
            finally:
                attempts_by_range[f"{b_start}-{b_end}"] = list(sink)

        writer_thread = threading.Thread(target=_delta_writer, daemon=True)
        writer_thread.start()
        batch_results: list[dict] = []
        batch_errors: list[tuple[tuple[int, int], LLMError]] = []
        # 失败区间（"start-end"）：写入 job.result，供前端与排障定位哪些集没解析成功。
        # 必须始终存在（全部成功时为 []），否则前端无法区分「没失败」与「不知道」。
        failed_ranges: list[str] = []
        max_workers = max(1, min(len(batches), int(getattr(settings, "llm_parse_concurrency", 3) or 3)))
        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dlab-parse") as pool:
                fut_map = {pool.submit(_parse_batch, bs, be): (bs, be) for bs, be in batches}
                for fut in as_completed(fut_map):
                    rng = fut_map[fut]
                    try:
                        batch_results.append(fut.result())
                    except job_cancel.JobCancelled:
                        # 任一分支被取消即整体取消：继续等其余批次只会白烧 token。
                        raise
                    except LLMError as e:
                        batch_errors.append((rng, e))
        finally:
            _writer_stop.set()
            writer_thread.join(timeout=5)

        if batch_errors:
            # 单集独立失败：成功集保留，仅标记失败集为 failed
            for (bs, be), e in batch_errors:
                # 失败批次同样要留下每次尝试的元数据：只有成功批写事件时，
                # 整批失败的任务在库里只剩一句「max_tokens=0」，无法判断到底
                # 是预算不足、供应商静默截断，还是思考链吃光了 token。
                atts = attempts_by_range.get(f"{bs}-{be}")
                if atts:
                    _write_event(db, job, "agent.parse_attempts", {
                        "range": f"{bs}-{be}",
                        "attempts": atts,
                        "failed": True,
                        "error_code": e.code,
                    })
                _upsert_records(db, job.project_id, bs, be - bs + 1, "failed",
                                error_msg=e.message[:2000], parse_version_id=pv.id)
                failed_ranges.append(f"{bs}-{be}")
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

        # ---- 镜头数对账（阶段 4：只警告，不阻断）----
        # 大纲 estimated_shots 是预估值。实际解析镜头数显著低于预估，通常意味着
        # 模型漏输出镜头（JSON 合法但镜头被省略/压缩）。只对**本批范围**对账：
        # 多批次增量解析时 per_episode_shots 只含本批集，越界比较会把未解析的集
        # 全部误报为 0。
        low_shot_episodes: list = []
        ratio_warn = float(getattr(settings, "parse_shot_ratio_warn", 0.5) or 0)
        if ratio_warn > 0:
            est_map: dict = {}
            for item in outline_data.get("episode_breakdown") or []:
                try:
                    est_map[int(item.get("episode") or 0)] = max(
                        1, int(item.get("estimated_shots") or 1))
                except (TypeError, ValueError, AttributeError):
                    continue
            for ep in range(episode_start, batch_end + 1):
                est = est_map.get(ep)
                if not est:
                    continue
                actual = per_episode_shots.get(ep, 0)
                ratio = actual / est
                if ratio < ratio_warn:
                    low_shot_episodes.append({
                        "episode": ep, "actual": actual,
                        "estimated": est, "ratio": round(ratio, 3),
                    })
                    parse_warnings.append(
                        f"shot_count_low: 第 {ep} 集实际 {actual} / 预估 {est} 镜"
                        f"（{ratio:.0%}），可能漏输出镜头"
                    )
            _write_event(db, job, "agent.shot_reconcile", {
                "range": f"{episode_start}-{batch_end}",
                "threshold": ratio_warn,
                "low_shot_episodes": low_shot_episodes,
            })
            db.commit()

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

        # 只对**实际成功**的批次区间写 succeeded。
        # 旧实现对整个 episode_start..episode_count 写 succeeded，会把上方刚标记的
        # failed 集覆盖成 succeeded——多批部分失败时，前端因此完全看不到失败集，
        # 还会给失败集写上成功批次的镜头数。
        for _bs, _be in sorted((br["start"], br["end"]) for br in batch_results):
            _upsert_records(db, job.project_id, _bs, _be - _bs + 1, "succeeded",
                            asset_counts=per_episode_shots, parse_version_id=pv.id)

        # ---- 覆盖率对账（基于 SourceIndex 与合并后的解析结果）----
        coverage_report = None
        try:
            from app.services.source_index import build_source_index
            from app.services.coverage import build_coverage_report
            si = build_source_index(text, script_version_id=sv.id, version=sv.version)
            coverage_report = build_coverage_report(si, merged).to_dict()
        except Exception:
            coverage_report = None
            parse_warnings.append("coverage_report: 覆盖率报告生成失败")

        # diff_summary 增量合并（多批次共用同一 ParseVersion）
        prev_summary = pv.diff_summary or {}
        # 批次完整性统计
        total_batches = len(batches)
        failed_batch_count = len(batch_errors)
        successful_batch_count = len(batch_results)
        # 解析集数：成功批次的集范围去重
        parsed_episode_nos: set[int] = set()
        for br in batch_results:
            for ep in range(br["start"], br["end"] + 1):
                parsed_episode_nos.add(ep)
        parsed_episodes = len(parsed_episode_nos)
        completion_status = "complete" if failed_batch_count == 0 else "partial"
        cov_status = coverage_report.get("overall", "passed") if coverage_report else "unknown"
        pv.diff_summary = {
            "characters": len(merged.get("characters") or []),
            "props": len(merged.get("props") or []),
            "scenes": len(merged.get("scenes") or []),
            "shots": (prev_summary.get("shots") or 0) + shot_total,
            "episodes": total_episodes,
            "parse_warnings": len(parse_warnings),
            "review_issues": len(review_issues),
            # 批次完整性字段（新增）
            "completion_status": completion_status,
            "expected_episodes": episode_count,
            "parsed_episodes": parsed_episodes,
            "successful_batches": successful_batch_count,
            "failed_batches": failed_batch_count,
            # 覆盖率状态
            "coverage_status": cov_status,
        }
        # 双写独立列（Schema 迁移过渡期）
        pv.completion_status = completion_status
        pv.expected_episodes = episode_count
        pv.parsed_episodes = parsed_episodes
        pv.successful_batches = successful_batch_count
        pv.failed_batches = failed_batch_count
        pv.coverage_status = cov_status

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
            "low_shot_episodes": low_shot_episodes,
            "failed_ranges": failed_ranges,
            "episode_start": episode_start,
            "episode_count": episode_count,
            "total_episodes": total_episodes,
            "has_more": has_more,
            "next_episode_start": batch_end + 1 if has_more else None,
            "evaluation": evaluation,
            "parse_version_id": pv.id,
            # 批次完整性标记（新增）：全部成功=complete，部分失败=partial
            "completion_status": completion_status,
            "successful_batches": successful_batch_count,
            "failed_batches": failed_batch_count,
            "parsed_episodes": parsed_episodes,
            # 覆盖率报告（新增）
            "coverage": coverage_report,
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
        # 结果回推发起会话：解析是异步任务，用户是在对话框里发起的；
        # 不回推时对话只会停在『正在分析』，用户得自己去页面找结果。
        # 复用 parse_executor 的同一实现，保证两条链路文案一致。
        try:
            from app.worker.parse_executor import _push_result_to_session
            _push_result_to_session(db, job, pv, merged, shot_total)
        except Exception:  # noqa: BLE001
            pass  # 回推失败不影响解析结果落库
        db.commit()
        return {"ok": True, "parse_version_id": pv.id, "shots": shot_total, "has_more": has_more}
    except job_cancel.JobCancelled as e:
        # 用户主动停止：终态必须是 cancelled，且**不落任何解析结果**。
        # 旧实现没有这条分支，取消后线程跑完会写 succeeded 并把半成品
        # 落进 ParseVersion —— 用户以为停了，数据却已经被改写。
        db.rollback()
        try:
            job = db.get(Job, job_id)
            if job is not None:
                job.status = "cancelled"
                job.finished_at = _now()
                job.result = {"cancelled": True, "reason": e.reason}
                _write_event(db, job, "agent.cancelled", {"reason": e.reason})
                db.commit()
        except Exception:
            db.rollback()
        return {"ok": False, "reason": "cancelled"}
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
        # 无论成功/失败/取消都要清理取消登记，否则同 id 复跑会被误判为已取消。
        job_cancel.clear(job_id)
        db.close()
