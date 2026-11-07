"""Search Service：搜索编排服务（联网搜索整改 Phase 2）。

设计约束（联网搜索整改方案 §4/§5/§7/§8）：
- 日志先行：每次搜索先落 SearchRun（pending）再执行，失败也留痕
- 状态机：pending → searching → validating → fetching → summarizing →
  success / partial / empty / failed / cancelled（终态不可变更）
- 空结果自动换关键词重试一次（不同搜索表达），仍无结果则 empty
- Provider 失败自动切换备用（web_search 内部已实现）
- 网页读取：对前 N 条有效结果读取正文（fetch_status 逐条落库）
- 搜索成功 ≠ 网页读取成功：读取部分失败 → partial，全部失败 → 保留结果但标记
- 返回结构化执行记录（search_run_id + 状态 + 结果 + 来源），供 payload 展示与审计
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import SearchRun, SearchResult
from app.models.ids import search_result_id, search_run_id
from app.services.search_validator import classify_fetch_status, validate_search_results
from app.services.web_fetch import web_fetch
from app.services.web_search import web_search

# 状态机合法流转（终态不可变更）
_SEARCH_TRANSITIONS = {
    "pending": {"searching", "failed", "cancelled"},
    "searching": {"validating", "empty", "failed", "cancelled"},
    "validating": {"fetching", "summarizing", "empty", "failed", "cancelled"},
    "fetching": {"summarizing", "failed", "cancelled"},
    "summarizing": {"success", "partial", "empty", "failed", "cancelled"},
    "success": set(),
    "partial": set(),
    "empty": set(),
    "failed": set(),
    "cancelled": set(),
}

# 网页读取条数上限（前 N 条有效结果）
FETCH_TOP_N = 3


def _transition(run: SearchRun, new_status: str) -> bool:
    """状态机门禁：非法流转拒绝。"""
    if new_status not in _SEARCH_TRANSITIONS.get(run.status, set()):
        return False
    run.status = new_status
    return True


def _normalize_query(user_query: str) -> str:
    """归一化搜索关键词：去指令性前缀，压缩空白。"""
    import re

    q = user_query.strip()
    # 去掉『帮我搜索/搜一下/查一下/联网搜索』等指令前缀
    q = re.sub(r"^(帮我|麻烦|请)?(联网)?(搜索|搜一下|查一下|查查|查询)", "", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q or user_query.strip()


def run_web_search(
    db: Session,
    user_query: str,
    *,
    session_id: str | None = None,
    project_id: str | None = None,
    max_results: int = 8,
) -> dict:
    """执行一次完整搜索（状态机 + 日志 + 验证 + 网页读取）。

    返回结构化执行记录：
    {search_run_id, status, query, provider, result_count, valid_result_count,
     fetched_count, duration_ms, results[{title,url,snippet,domain,content,fetch_status}],
     error}
    """
    started = time.monotonic()
    normalized = _normalize_query(user_query)
    # 日志先行：先落 pending 记录
    now = datetime.now()
    run = SearchRun(
        id=search_run_id(),
        session_id=session_id,
        project_id=project_id,
        user_query=user_query,
        normalized_query=normalized,
        status="pending",
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.commit()

    # 统一任务编排层：搜索任务接入统一 Task 层，状态机各阶段经 /api/tasks/events SSE 实时推送
    # （编排层失败不影响既有搜索链路）
    _task = None
    try:
        from app.services import task_orchestrator
        _task = task_orchestrator.create_task(
            db, session_id=session_id, task_type="web_search",
            title=f"联网搜索：{normalized[:60]}", project_id=project_id,
        )
        task_orchestrator.start_task(db, _task)
        db.commit()
    except Exception:  # noqa: BLE001
        _task = None
        db.rollback()

    _PHASE_PROGRESS = {"searching": 20, "validating": 45, "fetching": 60, "summarizing": 85}

    def _task_progress(phase: str) -> None:
        """状态机阶段 → 任务进度事件（SSE 实时推送）。"""
        if _task is None:
            return
        try:
            _task.progress = _PHASE_PROGRESS.get(phase, 0)
            task_orchestrator.write_event(
                db, _task, "task.progress",
                {"phase": phase, "progress": _PHASE_PROGRESS.get(phase, 0)},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()

    def _finish(status: str, *, error_code: str | None = None, error_message: str | None = None) -> dict:
        run.completed_at = datetime.now()
        run.duration_ms = int((time.monotonic() - started) * 1000)
        if error_code:
            run.error_code = error_code
        if error_message:
            run.error_message = error_message[:2000]
        _transition(run, status)
        db.commit()
        # 统一任务层闭环：success/partial → completed（含结果卡片）；failed/empty → failed
        if _task is not None:
            try:
                if status in ("success", "partial"):
                    task_orchestrator.complete_task(
                        db, _task, summary=f"搜索完成：{normalized}",
                        result_data=_run_payload(run),
                    )
                else:
                    task_orchestrator.fail_task(
                        db, _task,
                        error_code=error_code or status,
                        error_msg=error_message or status,
                    )
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        payload = _run_payload(run)
        # 供前端搜索卡片关联统一任务 SSE 快照（/api/tasks/events）实时更新
        if _task is not None:
            payload["task_id"] = _task.id
        return payload

    # ---- searching：执行搜索（Provider 故障切换在 web_search 内部）----
    _transition(run, "searching")
    db.commit()
    _task_progress("searching")
    outcome = web_search(normalized, max_results=max_results)
    run.attempt_count = 1
    run.search_provider = outcome.get("provider")

    # 空结果：换关键词重试一次（不同搜索表达）
    if outcome.get("status") == "empty":
        alt_query = f"{normalized} 介绍"
        outcome = web_search(alt_query, max_results=max_results)
        run.attempt_count = 2
        if outcome.get("provider"):
            run.search_provider = outcome.get("provider")

    if outcome.get("status") == "failed":
        run.result_count = 0
        return _finish(
            "failed",
            error_code="search_failed",
            error_message=str(outcome.get("error") or "所有搜索服务均失败"),
        )

    raw_results = outcome.get("results") or []
    run.result_count = len(raw_results)
    if not raw_results:
        return _finish("empty", error_code="empty_results", error_message="搜索无结果")

    # ---- validating：确定性验证（URL/标题/去重）----
    _transition(run, "validating")
    db.commit()
    _task_progress("validating")
    validated = validate_search_results(raw_results)
    run.valid_result_count = validated.valid_count
    if not validated.valid_results:
        return _finish(
            "empty",
            error_code="no_valid_results",
            error_message=f"{run.result_count} 条结果全部无效：{validated.removed_reasons}",
        )

    # ---- fetching：读取前 N 条正文 ----
    _transition(run, "fetching")
    db.commit()
    _task_progress("fetching")
    fetch_results: list[dict] = []
    enriched: list[dict] = []
    for i, item in enumerate(validated.valid_results):
        entry = dict(item)
        if i < FETCH_TOP_N:
            fr = web_fetch(item["url"])
            entry["content"] = fr.content if fr.status == "success" else None
            entry["fetch_status"] = fr.status
            fetch_results.append({"url": item["url"], "status": fr.status})
        else:
            entry["content"] = None
            entry["fetch_status"] = "pending"
        enriched.append(entry)

    # ---- 结果明细落库 ----
    now = datetime.now()
    for item in enriched:
        db.add(
            SearchResult(
                id=search_result_id(),
                search_run_id=run.id,
                title=item.get("title") or "",
                url=item.get("url") or "",
                domain=item.get("domain") or "",
                snippet=item.get("snippet") or "",
                published_at=item.get("published_at"),
                retrieved_at=now,
                fetch_status=item.get("fetch_status") or "pending",
                created_at=now,
                updated_at=now,
            )
        )

    # ---- summarizing → 终态 ----
    _transition(run, "summarizing")
    db.commit()
    _task_progress("summarizing")
    fetch_class = classify_fetch_status(fetch_results)
    if fetch_class == "failed":
        # 网页全部读取失败：保留搜索结果（URL/标题/摘要仍有效），标记 partial
        final_status = "partial"
    elif fetch_class == "partial":
        final_status = "partial"
    else:
        final_status = "success"
    return _finish(final_status)


def _run_payload(run: SearchRun) -> dict:
    """SearchRun 执行记录 → 结构化 payload（供前端展示与审计）。"""
    return {
        "search_run_id": run.id,
        "status": run.status,
        "query": run.normalized_query,
        "provider": run.search_provider,
        "result_count": run.result_count,
        "valid_result_count": run.valid_result_count,
        "attempt_count": run.attempt_count,
        "duration_ms": run.duration_ms,
        "error": run.error_message,
    }


def get_search_run_detail(db: Session, search_run_id: str) -> dict | None:
    """查询搜索执行记录详情（含结果明细），供历史搜索查询。"""
    run = db.get(SearchRun, search_run_id)
    if run is None:
        return None
    results = (
        db.query(SearchResult)
        .filter(SearchResult.search_run_id == search_run_id)
        .all()
    )
    payload = _run_payload(run)
    payload["results"] = [
        {
            "title": r.title,
            "url": r.url,
            "domain": r.domain,
            "snippet": r.snippet,
            "fetch_status": r.fetch_status,
        }
        for r in results
    ]
    return payload
