"""重新生成与替换工具（导演台 Agent 执行流程 P3：返工操作工具化）。

将返工类接口注册为受控工具，每个工具执行前经 pipeline_state 状态机门禁：
- regenerate_asset     资产级重新生成（角色/场景/道具图，结果落候选版本）
- replace_asset        上传新图原地替换资产（覆盖原文件，不产生新版本）
- regenerate_shot      镜头重新生成（分镜图/分镜视频，经重新生成工作台逻辑，结果落候选版本）
- adopt_candidate      采纳候选版本（原生效版本转历史，候选转生效）
- list_candidates      查询资产的候选版本列表（只读）

设计约束：
- 生成类操作（regenerate_asset/regenerate_shot）不直接执行：只创建审批申请，
  批准后经审批执行接口执行（与 request_generate 同纪律）
- 替换/采纳类操作（replace_asset/adopt_candidate）直接执行（有明确目标与确定性结果），
  但替换公共资产会重置资产审核闸口、替换分镜图会重置该镜人工确认（闸口联动由业务逻辑保证）
- 状态机门禁：操作不得早于其所属流程节点
- 工具返回真实执行结果与流程状态
"""
import time
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.models import Asset, Job, Project
from app.services.agent_tools import ToolError, ToolResult
from app.services.pipeline_state import PipelineStateError, assert_action_allowed, get_pipeline_status


# --- 入参 Schema ---

class RegenerateAssetInput(BaseModel):
    project_id: str
    asset_id: str  # ast_ 前缀，角色/场景/道具资产
    prompt: str | None = Field(default=None, max_length=2000)  # 缺省沿用默认提示词
    negative_prompt: str | None = Field(default=None, max_length=2000)


class ReplaceAssetInput(BaseModel):
    project_id: str
    asset_id: str
    file_path: str = Field(min_length=1, max_length=500)  # 服务器上已存在的替换文件路径


class RegenerateShotInput(BaseModel):
    project_id: str
    shot_id: str  # sht_ 前缀
    target: str = Field(default="keyframe", pattern="^(keyframe|clip)$")
    prompt: str | None = Field(default=None, max_length=2000)
    negative_prompt: str | None = Field(default=None, max_length=2000)


class AdoptCandidateInput(BaseModel):
    project_id: str
    asset_id: str  # 候选版本资产 ID


class ListCandidatesInput(BaseModel):
    project_id: str
    asset_id: str  # 当前生效版本资产 ID


# --- 辅助 ---

def _get_asset(db: Session, asset_id: str, project_id: str) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise ToolError("asset_not_found", f"资产不存在：{asset_id}")
    if asset.project_id != project_id:
        raise ToolError("asset_not_in_project", "该资产不属于当前项目")
    return asset


def _pipeline_ctx(db: Session, project_id: str) -> dict:
    st = get_pipeline_status(db, project_id)
    return {
        "pipeline_node": st.node_key,
        "pipeline_node_name": st.node_name,
        "stage": st.stage,
        "next_hint": st.next_hint,
    }


def _gate(action: str, db: Session, project_id: str) -> None:
    """状态机门禁：阻断时抛 ToolError（pipeline_gate_blocked）。"""
    try:
        assert_action_allowed(db, project_id, action)
    except PipelineStateError as e:
        raise ToolError("pipeline_gate_blocked", e.message)


# --- 工具实现 ---

def tool_regenerate_asset(db: Session, project_id: str, asset_id: str,
                           prompt: str | None = None,
                           negative_prompt: str | None = None) -> dict:
    """资产级重新生成（角色/场景/道具图）：按可编辑提示词重跑图片工作流，结果落候选版本。

    需人工审批：本工具只创建审批申请，批准后经审批执行接口执行。
    """
    asset = _get_asset(db, asset_id, project_id)
    if asset.asset_type not in ("character", "scene", "prop"):
        raise ToolError(
            "invalid_request",
            f"仅角色/场景/道具资产支持资产级重新生成，当前类型：{asset.asset_type}；"
            "分镜图/视频请用 regenerate_shot",
        )
    _gate("generate_character", db, project_id)
    if asset.locked:
        raise ToolError("asset_locked", "资产已锁定，请先解锁再重新生成")    # 复用 API 层实现（同构逻辑，避免重复维护提示词渲染与工作流匹配）
    from app.api.v1.assets import regenerate_asset as api_regenerate
    payload: dict = {}
    if prompt:
        payload["prompt"] = prompt
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    try:
        out = api_regenerate(asset_id, payload, db)
    except Exception as e:
        # HTTPException → 结构化错误（detail 含 code/message）
        detail = getattr(e, "detail", None)
        if isinstance(detail, dict):
            raise ToolError(detail.get("code", "tool_internal_error"), detail.get("message", "重新生成失败"))
        raise ToolError("tool_internal_error", "重新生成失败")
    return {
        "job_id": out["job_id"],
        "status": out["status"],
        "asset_id": asset_id,
        "base_version": out.get("base_version"),
        "note": "重新生成任务已入队，结果落候选版本；完成后可用 list_candidates 查看并 adopt_candidate 采纳",
        **_pipeline_ctx(db, project_id),
    }


def tool_replace_asset(db: Session, project_id: str, asset_id: str, file_path: str) -> dict:
    """替换资产图片：用服务器上已存在的文件原地替换（覆盖原文件，不产生新版本）。

    闸口联动：公共资产替换后原审核结论失效（需重新审核）；
    分镜图替换后该镜人工确认重置（需重新确认）。
    """
    _gate("generate_character", db, project_id)
    asset = _get_asset(db, asset_id, project_id)
    if asset.locked:
        raise ToolError("asset_locked", "资产已锁定，请先解锁再替换")
    from pathlib import Path
    src = Path(file_path)
    if not src.is_file():
        raise ToolError("file_not_found", f"替换文件不存在：{file_path}")
    # 复用 API 层实现（含文件类型校验、原地覆盖、缩略图重生成、闸口联动）
    import asyncio
    from fastapi import UploadFile
    from app.api.v1.assets import replace_asset as api_replace

    class _FileLike:
        def __init__(self, path: Path):
            self.filename = path.name
            self._path = path

        async def read(self) -> bytes:
            return self._path.read_bytes()

    upload = UploadFile(filename=src.name, file=None)  # type: ignore[arg-type]
    upload.filename = src.name
    # 直接构造异步读文件对象，绕过 UploadFile 的文件句柄依赖
    upload.read = _FileLike(src).read  # type: ignore[method-assign]
    try:
        out = asyncio.get_event_loop().run_until_complete(api_replace(asset_id, upload, db))
    except Exception as e:
        detail = getattr(e, "detail", None)
        if isinstance(detail, dict):
            raise ToolError(detail.get("code", "tool_internal_error"), detail.get("message", "替换失败"))
        raise ToolError("tool_internal_error", "替换失败")
    return {
        "asset_id": asset_id,
        "file_path": out.get("file_path"),
        "affected_shots": out.get("affected_shots", []),
        "note": "资产已原地替换；若为公共资产，原审核结论已失效需重新审核；若为分镜图，该镜需重新人工确认",
        **_pipeline_ctx(db, project_id),
    }


def tool_regenerate_shot(db: Session, project_id: str, shot_id: str,
                         target: str = "keyframe", prompt: str | None = None,
                         negative_prompt: str | None = None) -> dict:
    """镜头重新生成（分镜图/分镜视频）：沿用原配置仅改提示词，结果落候选版本。

    需人工审批：本工具只创建审批申请，批准后经审批执行接口执行。
    """
    from app.models import Shot, Scene
    shot = db.get(Shot, shot_id)
    if shot is None:
        raise ToolError("shot_not_found", f"镜头不存在：{shot_id}")
    scene = db.get(Scene, shot.scene_id)
    if scene is None or scene.project_id != project_id:
        raise ToolError("shot_not_in_project", "该镜头不属于当前项目")
    # 状态机门禁：分镜图须资产审核通过；视频须分镜图已确认
    _gate(f"generate_{target}", db, project_id)
    # 复用重新生成工作台 API（含基础配置读取、快照组装、任务入队）
    from app.api.v1.regeneration import post_regenerate
    payload: dict = {"target": target, "mode": "inherit_prompt_only"}
    if prompt:
        payload["prompt"] = prompt
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    try:
        out = post_regenerate(shot_id, payload, db)
    except Exception as e:
        detail = getattr(e, "detail", None)
        if isinstance(detail, dict):
            raise ToolError(detail.get("code", "tool_internal_error"), detail.get("message", "重新生成失败"))
        raise ToolError("tool_internal_error", "重新生成失败")
    return {
        "job_id": out.get("job_id"),
        "status": out.get("status"),
        "shot_id": shot_id,
        "target": target,
        "note": "重新生成任务已入队，结果落候选版本；完成后可用 list_candidates 查看并 adopt_candidate 采纳",
        **_pipeline_ctx(db, project_id),
    }


def tool_adopt_candidate(db: Session, project_id: str, asset_id: str) -> dict:
    """采纳候选版本：原生效版本转历史，候选转生效，并同步实体引用指针。"""
    asset = _get_asset(db, asset_id, project_id)
    pass  # 无版本概念，直接采纳
    from app.api.v1.regeneration import adopt_candidate as api_adopt
    try:
        out = api_adopt(asset_id, db)
    except Exception as e:
        detail = getattr(e, "detail", None)
        if isinstance(detail, dict):
            raise ToolError(detail.get("code", "tool_internal_error"), detail.get("message", "采纳失败"))
        raise ToolError("tool_internal_error", "采纳失败")
    return {
        "asset_id": asset_id,
        "adopted": out.get("adopted", asset_id),
        "superseded": out.get("superseded", []),
        "note": "候选版本已采纳为生效版本",
        **_pipeline_ctx(db, project_id),
    }


def tool_list_candidates(db: Session, project_id: str, asset_id: str) -> dict:
    """查询资产的候选版本列表（只读）：重新生成产生的候选版本，供采纳决策。"""
    _get_asset(db, asset_id, project_id)
    from app.api.v1.regeneration import list_candidates as api_list
    try:
        out = api_list(asset_id, db)
    except Exception as e:
        detail = getattr(e, "detail", None)
        if isinstance(detail, dict):
            raise ToolError(detail.get("code", "tool_internal_error"), detail.get("message", "查询失败"))
        raise ToolError("tool_internal_error", "查询失败")
    return {
        "asset_id": asset_id,
        "candidates": out.get("candidates", []),
        "count": len(out.get("candidates", [])),
    }


# --- 工具注册表 ---

REGEN_TOOL_REGISTRY: dict = {
    "regenerate_asset": {
        "description": "资产级重新生成（角色/场景/道具图）：按可编辑提示词重跑工作流，结果落候选版本（需人工审批）",
        "permission": "generate",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RegenerateAssetInput,
        "params": {
            "project_id": "项目 ID",
            "asset_id": "资产 ID（ast_ 前缀，角色/场景/道具）",
            "prompt": "可选：新提示词（缺省沿用默认）",
            "negative_prompt": "可选：负向提示词",
        },
        "fn": tool_regenerate_asset,
    },
    "replace_asset": {
        "description": "替换资产图片：用服务器上已存在的文件原地替换（覆盖原文件；公共资产替换后需重新审核，分镜图替换后需重新确认）",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": False,
        "requires_approval": False,
        "timeout_seconds": 60,
        "max_retries": 0,
        "input_model": ReplaceAssetInput,
        "params": {
            "project_id": "项目 ID",
            "asset_id": "资产 ID（ast_ 前缀）",
            "file_path": "服务器上的替换文件绝对路径",
        },
        "fn": tool_replace_asset,
    },
    "regenerate_shot": {
        "description": "镜头重新生成（分镜图/分镜视频）：沿用原配置仅改提示词，结果落候选版本（需人工审批）",
        "permission": "generate",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RegenerateShotInput,
        "params": {
            "project_id": "项目 ID",
            "shot_id": "镜头 ID（sht_ 前缀）",
            "target": "keyframe（分镜图）/ clip（分镜视频），默认 keyframe",
            "prompt": "可选：新提示词（缺省沿用原配置）",
            "negative_prompt": "可选：负向提示词",
        },
        "fn": tool_regenerate_shot,
    },
    "adopt_candidate": {
        "description": "采纳候选版本：原生效版本转历史，候选转生效，同步实体引用指针",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": False,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": AdoptCandidateInput,
        "params": {"project_id": "项目 ID", "asset_id": "候选版本资产 ID（ast_ 前缀）"},
        "fn": tool_adopt_candidate,
    },
    "list_candidates": {
        "description": "查询资产的候选版本列表（只读）：重新生成产生的候选版本，供采纳决策",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListCandidatesInput,
        "params": {"project_id": "项目 ID", "asset_id": "当前生效版本资产 ID（ast_ 前缀）"},
        "fn": tool_list_candidates,
    },
}


def execute_regen_tool(db: Session, name: str, *, approved: bool = False, **kwargs) -> ToolResult:
    """执行返工工具：审批门禁 → Schema 校验 → 状态机门禁（工具内）→ 执行 → 结构化结果。"""
    from pydantic import ValidationError
    start = time.monotonic()
    spec = REGEN_TOOL_REGISTRY.get(name)
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
        return ToolResult(ok=False, error_code=e.code, error_message=e.message,
                          duration_ms=int((time.monotonic() - start) * 1000))
    except ValidationError as e:
        return ToolResult(ok=False, error_code="tool_invalid_args", error_message=f"工具参数错误: {e}",
                          duration_ms=int((time.monotonic() - start) * 1000))
    except Exception:
        return ToolResult(ok=False, error_code="tool_internal_error", error_message="工具执行失败",
                          duration_ms=int((time.monotonic() - start) * 1000))


def regen_tool_specs() -> list[dict]:
    """返工工具元数据清单（供意图路由与文档展示）。"""
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
        for name, spec in REGEN_TOOL_REGISTRY.items()
    ]
