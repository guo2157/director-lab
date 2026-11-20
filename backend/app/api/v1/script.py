"""剧本与解析接口：API.md §5 契约。
- script-versions：保存剧本文本并创建不可变版本
- estimate：评估可制作集数（同步，不调语言模型，结果持久化）
- estimates/{id}/confirm：确认评估与制作范围
- parse：解析剧本（202 长任务，入队后由 worker 调语言模型）
- parse-versions/{id}/confirm：确认候选解析版本（乐观锁原子切换）
纪律：draft 项目首次调用 parse 自动激活（ARCHITECTURE.md §2）；
评估/解析均校验 text 哈希与 script_version_id 一致，不符 409 script_version_conflict。
"""
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import EpisodeParseRecord, Job, ParseVersion, Project, ScriptEstimate, ScriptVersion
from app.models import ids
from app.schemas.script import (
    EpisodeParseStatusItem, EstimateConfirm, EstimateConfirmOut, EstimateCreate, EstimateOut,
    ParseCreate, ParseJobOut, ParseRetryOut, ParseStatusOut, ParseVersionConfirm, ParseVersionOut,
    ScriptVersionCreate, ScriptVersionOut,
)
from app.services import errors
from app.services.script_parse import analyze_script_outline

router = APIRouter(prefix="/api/projects", tags=["script"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get_project(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise errors.project_not_found()
    return p


def _get_script_version(db: Session, sv_id: str) -> ScriptVersion:
    sv = db.get(ScriptVersion, sv_id)
    if sv is None:
        raise errors.script_version_not_found()
    return sv


def _check_text_hash(sv: ScriptVersion, text: str) -> None:
    if _hash(text) != sv.content_hash:
        raise errors.script_version_conflict("text 内容哈希与剧本版本不一致")


def _activate_if_draft(db: Session, p: Project) -> None:
    if p.status == "draft":
        p.status = "active"


@router.post("/{project_id}/script-versions", status_code=201)
def create_script_version(project_id: str, payload: ScriptVersionCreate,
                          db: Session = Depends(get_db)) -> ScriptVersionOut:
    """保存剧本（单版本覆盖）：每项目仅保留一条当前版本，保存时覆盖文本、版本号+1。"""
    p = _get_project(db, project_id)
    sv = (
        db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )
    if sv is None:
        sv = ScriptVersion(
            id=ids.script_version_id(), project_id=project_id, version=1,
            content_snapshot=payload.text, content_hash=_hash(payload.text), created_at=_now(),
        )
        db.add(sv)
        p.current_script_version_id = sv.id
    else:
        sv.content_snapshot = payload.text
        sv.content_hash = _hash(payload.text)
        sv.version += 1
        sv.created_at = _now()
        p.current_script_version_id = sv.id
    db.commit()
    return ScriptVersionOut(script_version_id=sv.id, version=sv.version, content_hash=sv.content_hash)


@router.get("/{project_id}/script-versions/current")
def get_current_script_version(project_id: str,
                                db: Session = Depends(get_db)) -> dict:
    """获取当前剧本（单版本覆盖模式）：返回文本与版本信息，供前端回显。"""
    _get_project(db, project_id)
    sv = (
        db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )
    if sv is None:
        return {"exists": False, "text": "", "script_version_id": None, "version": None}
    return {
        "exists": True,
        "text": sv.content_snapshot,
        "script_version_id": sv.id,
        "version": sv.version,
    }


@router.post("/{project_id}/script-normalize", status_code=200)
def normalize_project_script(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """剧本规范化校验（保存前）：把原始素材整理为标准剧本格式，保真前提下做必要补全。

    - 不落库：仅返回优化稿、变更清单与机器校验结果，由用户确认后再保存版本
    - 人物集合守恒与原台词保真由服务端机器校验，blocked=true 时前端需明确提示
    - LLM 失败按 llm_* 错误码返回 502，不产生半成品
    """
    from app.integrations.llm import LLMError
    from app.services.script_writing import normalize_script

    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可校验剧本")
    source_text = (payload.get("source_text") or "").strip()
    if not source_text:
        raise errors.invalid_request("source_text 必填（待校验的剧本素材）")
    if len(source_text) > 50000:
        raise errors.invalid_request("source_text 超过 50000 字符上限")
    try:
        result = normalize_script(source_text, p.settings or {})
    except LLMError as e:
        raise errors.AppError(e.code, e.message, 502)
    return result


@router.post("/{project_id}/script-write", status_code=201)
def write_project_script(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """AI 创作/扩写剧本（阶段1）：create（创意→剧本）/ expand（梗概→扩写）。

    - 创作配置取 Project.settings（genre/audience/visual_style/episode_count 等），
      修改配置后由 estimated_text_hash 机制驱动下游失效，不无条件清空资产
    - LLM 失败按 llm_* 错误码返回（不落库半成品）；成功后保存为新剧本版本
    - 已有完整剧本的用户直接走 script-versions 保存，不经本端点
    """
    from app.integrations.llm import LLMError
    from app.services.script_writing import write_script

    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可创作剧本")
    mode = payload.get("mode")
    if mode not in ("create", "expand"):
        raise errors.invalid_request("mode 须为 create 或 expand")
    source_text = (payload.get("source_text") or "").strip()
    if not source_text:
        raise errors.invalid_request("source_text 必填（创意或故事梗概）")
    if len(source_text) > 50000:
        raise errors.invalid_request("source_text 超过 50000 字符上限")
    # LLM 调用（短事务外，与 estimate 同模式）
    try:
        text = write_script(mode, source_text, p.settings or {})
    except LLMError as e:
        raise errors.AppError(e.code, e.message, 502)
    if not text or len(text) < 50:
        raise errors.AppError("llm_invalid_output", "生成剧本内容为空或过短", 502)
    # 保存为新剧本版本（复用 script-versions 覆盖式版本管理）
    sv = (
        db.query(ScriptVersion)
        .filter(ScriptVersion.project_id == project_id)
        .order_by(ScriptVersion.version.desc())
        .first()
    )
    if sv is None:
        sv = ScriptVersion(
            id=ids.script_version_id(), project_id=project_id, version=1,
            content_snapshot=text, content_hash=_hash(text), created_at=_now(),
        )
        db.add(sv)
    else:
        sv.content_snapshot = text
        sv.content_hash = _hash(text)
        sv.version += 1
        sv.created_at = _now()
    p.current_script_version_id = sv.id
    db.commit()
    return {
        "script_version_id": sv.id,
        "version": sv.version,
        "content_hash": sv.content_hash,
        "mode": mode,
        "text": text,
    }


@router.post("/{project_id}/script-evaluate", status_code=200)
def evaluate_project_script(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """剧本质量评估（阶段2）：调用 LLM 生成结构化评估报告。

    - 评估维度：优点、问题、修改建议、制作风险、剧情漏洞检测、总体评分、总结
    - 在 analyze_script_outline LLM 大纲分析基础上注入基础数据，LLM 进行深度质量评估
    - LLM 失败按 llm_* 错误码返回 502，不落库半成品
    - 评估为只读操作，不修改剧本版本或项目状态
    """
    from app.integrations.llm import LLMError
    from app.services.script_evaluation import evaluate_script

    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可评估剧本")
    script_version_id = payload.get("script_version_id")
    text = payload.get("text") or ""
    if not script_version_id:
        raise errors.invalid_request("script_version_id 必填")
    if not text.strip():
        raise errors.invalid_request("text 必填")
    sv = _get_script_version(db, script_version_id)
    if sv.project_id != project_id:
        raise errors.script_version_conflict("剧本版本不属于本项目")
    _check_text_hash(sv, text)
    # LLM 大纲分析作为基础数据注入（代码规则估算已移除，集数一律以 LLM 分析为准）
    settings_map = p.settings or {}
    try:
        estimate_data = analyze_script_outline(
            text,
            episode_duration_target=int(settings_map.get("episode_duration_target", 90)),
            max_episodes_per_batch=int(settings_map.get("max_episodes_per_batch") or get_settings().max_episodes_per_batch),
            settings=get_settings(),
        )
    except LLMError as e:
        raise errors.AppError(e.code, e.message, 502)
    # LLM 深度评估（短事务外）
    try:
        report = evaluate_script(
            text, estimate_data, p.settings or {},
            settings=get_settings(),
        )
    except LLMError as e:
        raise errors.AppError(e.code, e.message, 502)
    return {
        "script_version_id": sv.id,
        "estimate_data": estimate_data,
        "report": report,
    }


@router.post("/{project_id}/estimate", status_code=201)
def create_estimate(project_id: str, payload: EstimateCreate,
                    db: Session = Depends(get_db)) -> EstimateOut:
    from app.integrations.llm import LLMError
    p = _get_project(db, project_id)
    sv = _get_script_version(db, payload.script_version_id)
    if sv.project_id != project_id:
        raise errors.script_version_conflict("剧本版本不属于本项目")
    _check_text_hash(sv, payload.text)
    settings_map = p.settings or {}
    try:
        result = analyze_script_outline(
            payload.text,
            episode_duration_target=int(settings_map.get("episode_duration_target", 90)),
            max_episodes_per_batch=int(settings_map.get("max_episodes_per_batch") or get_settings().max_episodes_per_batch),
        )
    except LLMError as e:
        raise errors.AppError(e.code, e.message, 502)
    est = ScriptEstimate(
        id=ids.script_estimate_id(), script_version_id=sv.id,
        algorithm_version=result["algorithm_version"], result=result,
        user_confirmed=False, created_at=_now(),
    )
    db.add(est)
    p.estimated_at = _now()
    p.estimated_text_hash = sv.content_hash
    db.commit()
    return EstimateOut(
        estimate_id=est.id, script_version_id=sv.id,
        algorithm_version=result["algorithm_version"],
        char_count=result["char_count"], dialogue_char_estimate=result["dialogue_char_estimate"],
        estimated_total_duration=result["estimated_total_duration"],
        estimated_episodes=result["estimated_episodes"],
        episode_breakdown=result["episode_breakdown"],
        exceeds_batch_limit=result["exceeds_batch_limit"],
        suggestion=result["suggestion"], user_confirmed=False,
    )


@router.post("/{project_id}/estimates/{estimate_id}/confirm")
def confirm_estimate(project_id: str, estimate_id: str, payload: EstimateConfirm,
                     db: Session = Depends(get_db)) -> EstimateConfirmOut:
    _get_project(db, project_id)
    est = db.get(ScriptEstimate, estimate_id)
    if est is None:
        raise errors.estimate_not_found()
    if est.script_version_id != payload.script_version_id:
        raise errors.script_version_conflict("评估版本与确认的剧本版本不一致")
    # 已锁定的评估不可重复确认改集数（口径锁定）
    if est.locked_at is not None and est.total_episodes is not None:
        if payload.episode_count > est.total_episodes:
            raise errors.invalid_request(
                f"评估已锁定总集数 {est.total_episodes}，解析范围不可超出"
            )
    est.user_confirmed = True
    # 首次确认时锁定总集数快照：此后解析范围不可超出，保证多轮解析口径一致
    if est.total_episodes is None:
        est.total_episodes = payload.episode_count
        est.locked_at = _now()
    db.commit()
    return EstimateConfirmOut(
        estimate_id=est.id, user_confirmed=True,
        episode_start=payload.episode_start, episode_count=payload.episode_count,
        total_episodes=est.total_episodes, locked=True,
    )


@router.post("/{project_id}/script-agent", status_code=202)
def create_script_agent(project_id: str, payload: dict, db: Session = Depends(get_db)) -> dict:
    """剧本一体化分析（Agent）：评估+解析合并，集数由 Agent 决定，无需用户确认集数。

    - 入参与 parse 一致：script_version_id + text（hash 校验）
    - episode_start 可选（默认 1；后续批次继续分析时传入）
    - 首批自动解析，后续批次由前端『继续分析』推进
    """
    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可分析剧本")
    script_version_id = payload.get("script_version_id")
    text = payload.get("text") or ""
    if not script_version_id:
        raise errors.invalid_request("script_version_id 必填")
    if not text.strip():
        raise errors.invalid_request("text 必填")
    sv = _get_script_version(db, script_version_id)
    if sv.project_id != project_id:
        raise errors.script_version_conflict("剧本版本不属于本项目")
    _check_text_hash(sv, text)
    # 输入预算预检（分级放行）：同 create_parse，单集放不下才拒绝，
    # 仅整本超预算的剧本放行并由分段大纲处理
    from app.integrations.token_estimate import precheck_input_budget
    _pre = precheck_input_budget(text, settings=get_settings(), allow_segmented=True)
    if not _pre["ok"]:
        raise errors.AppError(_pre["code"], _pre["message"], 400)
    episode_start = int(payload.get("episode_start") or 1)
    if episode_start < 1:
        raise errors.invalid_request("episode_start 不得小于 1")
    # 幂等拦截（2026-10-28）：剧本未变化（同 script_version_id）且同 episode_start 已有成功的
    # script_agent 任务时，直接复用已有结果，不创建新任务、不重复调用 LLM。
    # 剧本 hash 变化会生成新 script_version_id，自然走重新分析；force=true 可强制重析。
    if not payload.get("force"):
        existing = (
            db.query(Job)
            .filter(
                Job.project_id == project_id,
                Job.job_type == "script_agent",
                Job.status == "succeeded",
            )
            .order_by(Job.created_at.desc())
            .all()
        )
        for j in existing:
            jp = j.params or {}
            if jp.get("script_version_id") == sv.id and int(jp.get("episode_start") or 1) == episode_start:
                return {
                    "job_id": j.id,
                    "status": j.status,
                    "idempotent_reuse": True,
                    "message": "剧本未变化且该批次已成功分析，已复用已有结果（如需重新分析请传 force=true）",
                }
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="script_agent",
        params={
            "script_version_id": sv.id,
            "episode_start": episode_start,
            # 建任务时的预检结论：full=整本送大纲，segmented=按集分窗口
            "outline_mode": _pre["outline_mode"],
        },
        status="queued", created_at=_now(),
    )
    db.add(job)
    _activate_if_draft(db, p)
    db.commit()
    return {"job_id": job.id, "status": job.status}


@router.get("/{project_id}/script-agent/status")
def get_script_agent_status(project_id: str, db: Session = Depends(get_db)) -> dict:
    """Agent 分析状态：最近一个 script_agent 任务 + 集级记录 + 口径信息。"""
    _get_project(db, project_id)
    job = (
        db.query(Job)
        .filter(Job.project_id == project_id, Job.job_type == "script_agent")
        .order_by(Job.created_at.desc())
        .first()
    )
    records = (
        db.query(EpisodeParseRecord)
        .filter(EpisodeParseRecord.project_id == project_id,
                EpisodeParseRecord.asset_type == "all")
        .order_by(EpisodeParseRecord.episode_no)
        .all()
    )
    # 口径：最新 candidate/succeeded 解析版本
    pv = (
        db.query(ParseVersion)
        .filter(ParseVersion.project_id == project_id)
        .order_by(ParseVersion.version.desc())
        .first()
    )
    out = {
        "job": {
            "id": job.id, "status": job.status,
            "error_code": job.error_code, "error_msg": job.error_msg,
            "result": job.result,
        } if job else None,
        "scope": {
            "total_episodes": pv.total_episodes if pv else None,
            "script_hash": pv.script_hash if pv else None,
            "parse_version_id": pv.id if pv else None,
            "parse_version_status": pv.status if pv else None,
        },
        "records": [
            {
                "episode_no": r.episode_no,
                "status": r.status,
                "asset_count": r.asset_count,
                "error_msg": r.error_msg,
            }
            for r in records
        ],
    }
    return out


@router.post("/{project_id}/parse", status_code=202)
def create_parse(project_id: str, payload: ParseCreate,
                 db: Session = Depends(get_db)) -> ParseJobOut:
    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可解析")
    sv = _get_script_version(db, payload.script_version_id)
    if sv.project_id != project_id:
        raise errors.script_version_conflict("剧本版本不属于本项目")
    _check_text_hash(sv, payload.text)
    # 输入预算预检（分级放行）：
    # - 单集正文都放不下 → 建任务前直接 400（任何切分都救不回来，避免
    #   用户看到一个「排队 → 失败」的无效任务）
    # - 仅整本超预算、单集放得下 → 放行，由分段大纲按集切窗口处理
    from app.integrations.token_estimate import precheck_input_budget
    _pre = precheck_input_budget(payload.text, settings=get_settings(), allow_segmented=True)
    if not _pre["ok"]:
        raise errors.AppError(_pre["code"], _pre["message"], 400)
    # 解析区间归一化：episode_count/episode_end 可选（LLM parser 自主分析集数）
    # 未提供时默认从第1集开始，episode_count 传 None 由 parser 自行确定
    if payload.episode_count is not None and payload.episode_end is not None:
        raise errors.invalid_request("episode_count 与 episode_end 只能提供其一")
    if payload.episode_end is not None:
        if payload.episode_end < payload.episode_start:
            raise errors.invalid_request("episode_end 不得小于 episode_start")
        episode_count = payload.episode_end - payload.episode_start + 1
    else:
        episode_count = payload.episode_count
    job = Job(
        id=ids.job_id(), project_id=project_id, job_type="script_agent",
        params={
            "script_version_id": sv.id,
            "episode_start": payload.episode_start,
            "episode_count": episode_count,
            # 建任务时的预检结论：full=整本送大纲，segmented=按集分窗口
            "outline_mode": _pre["outline_mode"],
        },
        status="queued", created_at=_now(),
    )
    db.add(job)
    _activate_if_draft(db, p)
    db.commit()
    return ParseJobOut(job_id=job.id, status=job.status)


@router.get("/{project_id}/parse/status")
def get_parse_status(project_id: str, db: Session = Depends(get_db)) -> ParseStatusOut:
    """集级解析状态查询：返回各集解析记录，供前端集数网格视图渲染。"""
    _get_project(db, project_id)
    # 锁定的总集数：仅从 ParseVersion 获取（LLM parser 自主分析集数后回写）
    pv_scope = (
        db.query(ParseVersion)
        .filter(ParseVersion.project_id == project_id, ParseVersion.total_episodes.isnot(None))
        .order_by(ParseVersion.version.desc())
        .first()
    )
    total_episodes = pv_scope.total_episodes if pv_scope is not None else None
    records = (
        db.query(EpisodeParseRecord)
        .filter(EpisodeParseRecord.project_id == project_id)
        .order_by(EpisodeParseRecord.episode_no, EpisodeParseRecord.asset_type)
        .all()
    )
    return ParseStatusOut(
        total_episodes=total_episodes,
        records=[
            EpisodeParseStatusItem(
                episode_no=r.episode_no, asset_type=r.asset_type, status=r.status,
                attempt_count=r.attempt_count or 0, error_msg=r.error_msg,
                asset_count=r.asset_count, parse_version_id=r.parse_version_id,
                updated_at=r.updated_at,
            )
            for r in records
        ],
    )


@router.post("/{project_id}/parse/retry", status_code=202)
def retry_parse(project_id: str, db: Session = Depends(get_db)) -> ParseRetryOut:
    """重试失败的集级解析记录：仅 failed 状态的集重新入队，成功/进行中的不动。"""
    p = _get_project(db, project_id)
    if p.status == "archived":
        raise errors.invalid_state_transition("已归档项目不可解析")
    failed = (
        db.query(EpisodeParseRecord)
        .filter(
            EpisodeParseRecord.project_id == project_id,
            EpisodeParseRecord.status == "failed",
        )
        .order_by(EpisodeParseRecord.episode_no)
        .all()
    )
    if not failed:
        raise errors.invalid_request("没有失败的解析记录可重试")
    # 失败集去重，每集独立创建 script_agent 重试任务，避免合并区间覆盖成功集
    episodes = sorted({r.episode_no for r in failed})
    sv = db.get(ScriptVersion, p.current_script_version_id) if p.current_script_version_id else None
    if sv is None:
        raise errors.script_version_not_found()
    job_ids: list[str] = []
    for ep in episodes:
        job = Job(
            id=ids.job_id(), project_id=project_id, job_type="script_agent",
            params={
                "script_version_id": sv.id,
                "episode_start": ep,
                "episode_count": 1,
                "retry": True,
                "retry_episodes": [ep],
            },
            status="queued", created_at=_now(),
        )
        db.add(job)
        job_ids.append(job.id)
    db.commit()
    return ParseRetryOut(job_id=job_ids[0] if job_ids else "", retried_episodes=episodes)


@router.post("/{project_id}/parse-versions/{parse_version_id}/confirm")
def confirm_parse_version(project_id: str, parse_version_id: str, payload: ParseVersionConfirm,
                          db: Session = Depends(get_db)) -> ParseVersionOut:
    p = _get_project(db, project_id)
    pv = db.get(ParseVersion, parse_version_id)
    if pv is None or pv.project_id != project_id:
        raise errors.parse_version_not_found()
    if pv.status == "candidate":
        pass
    elif pv.status == "current":
        # script-agent 多批次：首批确认后版本为 current，后续批次复用同一版本做增量确认；
        # 仅当存在该版本的 script_agent 成功任务时允许，普通 parse 流程的重复确认仍拒绝
        _agent_jobs = (
            db.query(Job)
            .filter(Job.project_id == project_id,
                    Job.job_type == "script_agent",
                    Job.status == "succeeded")
            .all()
        )
        if not any((j.result or {}).get("parse_version_id") == pv.id for j in _agent_jobs):
            raise errors.parse_version_conflict("仅候选状态的解析版本可确认")
    else:
        raise errors.parse_version_conflict("仅候选状态的解析版本可确认")
    # 乐观锁：请求携带的当前版本须与实际一致
    if (payload.current_parse_version_id or None) != (p.current_parse_version_id or None):
        raise errors.parse_version_conflict()
    # 找到产生该候选版本的解析任务（parse 或 script_agent），取其 result 做级联落库；
    # script-agent 多批次共用同一 ParseVersion，须按 episode_start 升序逐批应用
    parse_jobs = (
        db.query(Job)
        .filter(Job.project_id == project_id,
                Job.job_type.in_(["parse", "script_agent"]),
                Job.status == "succeeded")
        .order_by(Job.finished_at.desc())
        .all()
    )
    # 应用顺序：(episode_start, finished_at) 升序——同集数范围时旧任务先应用、
    # 新批次后应用覆盖；否则旧全量解析会覆盖新批次的增量结果
    target_results = [
        (j.result, j.finished_at.isoformat() if j.finished_at else "")
        for j in parse_jobs if (j.result or {}).get("parse_version_id") == pv.id
    ]
    target_results.sort(key=lambda t: (int(t[0].get("episode_start") or 1), t[1]))
    target_results = [t[0] for t in target_results]
    if target_results:
        from app.models import Scene as SceneModel
        from app.services.script_parse import apply_parse_result
        current_scene_ids = [
            row[0] for row in db.query(SceneModel.id).filter(SceneModel.project_id == project_id).all()
        ]
        for target_result in target_results:
            apply_parse_result(
                db, p, target_result,
                episode_start=int(target_result.get("episode_start") or 1),
                episode_count=int(target_result.get("episode_count") or 1),
                current_scene_ids=current_scene_ids,
            )
    # 旧版本标记 superseded
    old_id = p.current_parse_version_id
    if old_id:
        old = db.get(ParseVersion, old_id)
        if old is not None and old.status == "current":
            old.status = "superseded"
    pv.status = "current"
    pv.confirmed_at = _now()
    p.current_parse_version_id = pv.id
    db.commit()
    return ParseVersionOut(
        parse_version_id=pv.id, version=pv.version, status=pv.status, diff_summary=pv.diff_summary,
    )
