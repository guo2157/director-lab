"""Agent 受控工具注册表（Schema 化版本，单 Agent 核心流程框架 P1-1）。

设计约束（AGENTS.md P0 / 任务书 §8.4）：
- 工具只能由确定性业务代码执行，模型只负责选择与解释
- 首批仅注册只读查询工具，无任何写操作/生成触发（失效传播只标记不触发）
- 每个工具：名称/用途/权限等级/风险等级/副作用/幂等性/审批要求/超时/重试/
  Pydantic 入参 Schema/结构化返回/错误分类
- 工具返回真实数据库查询结果，禁止模拟数据
- 可信参数（project_id 等会话上下文）由服务端注入，不由模型决定

权限分级：
- permission：read（只读）/ write（写操作）/ generate（触发生成任务）
- risk_level：low / medium / high（高风险工具须人工审批）
- side_effect：工具是否改变系统状态
- idempotent：重复执行是否安全
- requires_approval：不可逆/高影响操作须人工审批后才执行
- execute_tool 对 requires_approval=True 且未获批（approved=False）的工具返回
  approval_required，不产生任何副作用；审批事件由会话层以
  AgentMessage(role="approval") 记录，获批后以 approved=True 重新调用
"""
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Job, Project
from app.services.progress import derive_progress


# 工具权限等级白名单（注册表校验用）
TOOL_PERMISSION_LEVELS = {"read", "write", "generate"}
# 风险等级白名单
TOOL_RISK_LEVELS = {"low", "medium", "high"}


# --- 入参 Schema（Pydantic，模型可见 JSON Schema，服务端执行校验） ---


class ProjectOverviewInput(BaseModel):
    """project_overview 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


class ListJobsInput(BaseModel):
    """list_jobs 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")
    status: str | None = Field(
        default=None,
        description="可选：任务状态过滤（queued/running/succeeded/failed/cancelled）",
    )
    limit: int = Field(default=20, ge=1, le=50, description="可选：返回条数上限，默认 20，最大 50")


class AssetStatsInput(BaseModel):
    """asset_stats 入参。"""

    project_id: str = Field(min_length=1, description="项目 ID（prj_ 前缀）")


@dataclass
class ToolResult:
    """工具执行结果：ok=False 时携带错误码与用户可读信息。"""

    ok: bool
    data: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- 工具实现（全部只读） ---


def tool_project_overview(db: Session, project_id: str) -> dict:
    """项目概览：基础信息 + 派生进度（镜头总数/完成数/当前阶段）。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    total, done, stage = derive_progress(db, p)
    return {
        "id": p.id,
        "name": p.name,
        "status": p.status,
        "aspect_ratio": p.aspect_ratio,
        "current_stage": stage,
        "shot_total": total,
        "shot_done": done,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def tool_list_jobs(db: Session, project_id: str, status: str | None = None, limit: int = 20) -> dict:
    """项目任务列表（最近优先）：id/类型/目标/状态/错误码。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    q = select(Job).where(Job.project_id == project_id)
    if status:
        q = q.where(Job.status == status)
    jobs = (
        db.scalars(q.order_by(Job.created_at.desc()).limit(min(max(1, limit), 50))).unique().all()
    )
    return {
        "items": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "shot_id": j.shot_id,
                "status": j.status,
                "error_code": j.error_code,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in jobs
        ],
        "count": len(jobs),
    }


def tool_asset_stats(db: Session, project_id: str) -> dict:
    """项目资产统计：按 asset_type 分组计数与最新资产时间。"""
    p = db.get(Project, project_id)
    if not p:
        raise ToolError("project_not_found", "项目不存在")
    rows = db.execute(
        select(Asset.asset_type, func.count(Asset.id))
        .where(Asset.project_id == project_id)
        .group_by(Asset.asset_type)
    ).all()
    return {
        "project_id": project_id,
        "by_type": {row[0]: row[1] for row in rows},
        "total": sum(row[1] for row in rows),
    }


# --- 注册表（Schema 化：每工具含 input_model 与风险/超时/重试元数据） ---

TOOL_REGISTRY: dict = {
    "project_overview": {
        "description": "查询项目概览：基础信息、当前制作阶段、镜头进度",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectOverviewInput,
        "params": {"project_id": "项目 ID（prj_ 前缀）"},
        "fn": tool_project_overview,
    },
    "list_jobs": {
        "description": "查询项目任务列表：生成任务的状态与错误信息（可按状态过滤）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListJobsInput,
        "params": {
            "project_id": "项目 ID",
            "status": "可选：任务状态过滤（queued/running/succeeded/failed/cancelled）",
            "limit": "可选：返回条数上限，默认 20，最大 50",
        },
        "fn": tool_list_jobs,
    },
    "asset_stats": {
        "description": "查询项目资产统计：按类型（角色图/场景图/道具图/分镜图/视频等）计数",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": AssetStatsInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_asset_stats,
    },
}

# 意图 → 候选工具映射（只向模型暴露当前意图需要的工具，降低选错/越权/Token 消耗）
INTENT_TOOL_MAPPING: dict[str, list[str]] = {
    "query": ["project_overview", "list_jobs", "asset_stats"],
    "analysis": ["project_overview", "asset_stats", "list_jobs"],
}


def get_allowed_tools(intent: str) -> list[str]:
    """根据意图返回候选工具名列表（未映射意图返回空列表，由调用方决定降级策略）。"""
    return list(INTENT_TOOL_MAPPING.get(intent, []))


def tool_specs() -> list[dict]:
    """工具元数据清单（供意图路由与文档展示，不含可调用函数）。"""
    return [
        {
            "name": name,
            "description": spec["description"],
            "permission": spec["permission"],
            "risk_level": spec.get("risk_level", "low"),
            "side_effect": spec["side_effect"],
            "idempotent": spec["idempotent"],
            "requires_approval": spec["requires_approval"],
            "timeout_seconds": spec.get("timeout_seconds", 30),
            "max_retries": spec.get("max_retries", 0),
            "params": spec["params"],
            "input_schema": spec["input_model"].model_json_schema() if spec.get("input_model") else None,
        }
        for name, spec in TOOL_REGISTRY.items()
    ]


def execute_tool(db: Session, name: str, *, approved: bool = False, **kwargs) -> ToolResult:
    """执行注册工具：审批门禁 → Schema 参数校验 → 执行 → 结构化结果。

    - requires_approval=True 且 approved=False：返回 approval_required，不执行、无副作用
    - 入参经 input_model.model_validate 校验，ValidationError → tool_invalid_args
    - 未知工具/执行错误分类返回，不外泄内部细节
    """
    start = time.monotonic()
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return ToolResult(ok=False, error_code="tool_not_found", error_message=f"未注册的工具: {name}")
    if spec.get("requires_approval") and not approved:
        return ToolResult(
            ok=False,
            error_code="approval_required",
            error_message=f"工具 {name} 需要人工审批后执行",
            data={"tool": name, "params": kwargs, "permission": spec.get("permission")},
        )
    input_model = spec.get("input_model")
    try:
        if input_model is not None:
            validated = input_model.model_validate(kwargs)
            data = spec["fn"](db, **validated.model_dump())
        else:
            data = spec["fn"](db, **kwargs)
        return ToolResult(ok=True, data=data, duration_ms=int((time.monotonic() - start) * 1000))
    except ToolError as e:
        return ToolResult(ok=False, error_code=e.code, error_message=e.message, duration_ms=int((time.monotonic() - start) * 1000))
    except ValidationError as e:
        return ToolResult(ok=False, error_code="tool_invalid_args", error_message=f"工具参数错误: {e}", duration_ms=int((time.monotonic() - start) * 1000))
    except TypeError as e:
        return ToolResult(ok=False, error_code="tool_invalid_args", error_message=f"工具参数错误: {e}", duration_ms=int((time.monotonic() - start) * 1000))
    except Exception:  # 工具内部错误不外泄细节，仅分类码
        return ToolResult(ok=False, error_code="tool_internal_error", error_message="工具执行失败", duration_ms=int((time.monotonic() - start) * 1000))
