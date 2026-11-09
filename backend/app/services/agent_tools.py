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
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, Character, Job, Project, Prop, Scene, Shot, Workflow, ids
from app.models.asset_plan import AssetPlan, AssetPlanDependency
from app.services.progress import derive_progress
from collections.abc import Callable


# 工具权限等级白名单（注册表校验用）
TOOL_PERMISSION_LEVELS = {"read", "write", "generate"}
# 风险等级白名单
TOOL_RISK_LEVELS = {"low", "medium", "high"}


from app.services.agent_tool_impls import (  # noqa: F401 向后兼容再导出
    BatchRequestGenerateInput,
    GetShotPromptInput,
    ScriptCheckInput,
    tool_batch_request_generate,
    tool_get_shot_prompt,
    tool_script_evaluate,
    tool_script_normalize,
    AssetStatsInput,
    GetJobInput,
    GetParseStatusInput,
    GetShotImpactInput,
    GetStyleConfigInput,
    ListAssetPlansInput,
    ListAssetVersionsInput,
    ListAssetsInput,
    ListCompositionsInput,
    ListExportsInput,
    ListJobsInput,
    ListProjectEntitiesInput,
    AddMemoryInput,
    ListMemoriesInput,
    ListProjectShotsInput,
    ProjectOverviewInput,
    ProjectProgressInput,
    RequestGenerateInput,
    RunGenerateInput,
    RunStyleUpdateInput,
    SupersedeMemoryInput,
    ToolError,
    ToolResult,
    UpdateStyleConfigInput,
    tool_add_memory,
    tool_asset_stats,
    tool_get_asset_generation_progress,
    tool_get_clip_generation_progress,
    tool_get_job,
    tool_get_keyframe_generation_progress,
    tool_get_parse_status,
    tool_get_shot_impact,
    tool_get_style_config,
    tool_list_asset_plans,
    tool_list_asset_versions,
    tool_list_assets,
    tool_list_compositions,
    tool_list_exports,
    tool_list_jobs,
    tool_list_memories,
    tool_list_project_entities,
    tool_list_project_shots,
    tool_project_overview,
    tool_request_generate,
    tool_request_style_update,
    tool_run_generate,
    tool_run_style_update,
    tool_supersede_memory,
)


# ── 工具前置条件（Phase 2） ──────────────────────────────────────────
# 前置条件签名为 (db: Session, **kwargs) -> tuple[bool, str]
# 返回 (True, "") 表示通过；(False, "阻塞原因") 表示不满足
ToolPreconditionFn = Callable[[Session, dict], tuple[bool, str]]


def _precheck_project_exists(db: Session, **kwargs: object) -> tuple[bool, str]:
    """项目必须存在。"""
    project_id = str(kwargs.get("project_id", "") or "")
    if not project_id or not db.get(Project, project_id):
        return False, "项目不存在"
    return True, ""


def _precheck_script_exists(db: Session, **kwargs: object) -> tuple[bool, str]:
    """项目必须有已保存的剧本。"""
    project_id = str(kwargs.get("project_id", "") or "")
    p = db.get(Project, project_id)
    if not p:
        return False, "项目不存在"
    if not p.current_script_version_id:
        return False, "项目尚未保存剧本，请先保存剧本后再执行此操作"
    return True, ""


def _precheck_script_parsed(db: Session, **kwargs: object) -> tuple[bool, str]:
    """剧本必须已解析并产出镜头。"""
    from app.models import ParseVersion

    project_id = str(kwargs.get("project_id", "") or "")
    p = db.get(Project, project_id)
    if not p:
        return False, "项目不存在"
    if not p.current_parse_version_id:
        return False, "剧本尚未完成解析，请先执行剧本分析后再操作"
    pv = db.get(ParseVersion, p.current_parse_version_id)
    if not pv or pv.status not in ("completed",):
        return False, f"剧本解析状态为 {pv.status if pv else '未知'}，请等待解析完成后再操作"
    return True, ""


def _precheck_assets_exist(db: Session, **kwargs: object) -> tuple[bool, str]:
    """项目必须有已生成的资产（角色/场景/道具）。"""
    project_id = str(kwargs.get("project_id", "") or "")
    if not db.get(Project, project_id):
        return False, "项目不存在"
    asset_count = db.scalar(
        select(func.count(Asset.id)).where(Asset.project_id == project_id)
    )
    if not asset_count:
        return False, "项目尚无资产，请先生成角色/场景/道具图后再操作"
    return True, ""


TOOL_REGISTRY: dict = {
    "get_shot_prompt": {
        "description": "查看分镜生成入参：镜头存储提示词（图/视频/台词）与编译后分镜图提示词预览",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetShotPromptInput,
        "params": {
            "project_id": "项目 ID（prj_ 前缀）",
            "shot_id": "镜头 ID（sht_ 前缀）；与 shot_seq 二选一",
            "episode": "集号（按镜号定位时提供）",
            "shot_seq": "镜号（与 shot_id 二选一）",
        },
        "fn": tool_get_shot_prompt,
    },
    "script_normalize": {
        "description": "剧本规范化校验（只读不落库）：对当前剧本版本生成优化稿与变更清单，用户采纳后再保存",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 300,
        "max_retries": 0,
        "input_model": ScriptCheckInput,
        "params": {"project_id": "项目 ID（prj_ 前缀）"},
        "preconditions": [_precheck_project_exists, _precheck_script_exists],
        "fn": tool_script_normalize,
    },
    "script_evaluate": {
        "description": "剧本质量评估（只读）：对当前剧本版本生成结构化评估报告（评分/问题/建议/制作风险）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 300,
        "max_retries": 0,
        "input_model": ScriptCheckInput,
        "params": {"project_id": "项目 ID（prj_ 前缀）"},
        "preconditions": [_precheck_project_exists, _precheck_script_exists],
        "fn": tool_script_evaluate,
    },
    "batch_request_generate": {
        "description": "批量提交公共资产生成审批申请（只建申请不执行）：逐实体校验后批量建审批，批准后逐条执行",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 60,
        "max_retries": 0,
        "input_model": BatchRequestGenerateInput,
        "params": {
            "session_id": "会话 ID（服务端注入）",
            "project_id": "项目 ID（prj_ 前缀）",
            "entity_ids": "实体 ID 列表（chr_/scn_/prp_ 前缀，最多 20 个）",
        },
        "preconditions": [_precheck_project_exists, _precheck_script_parsed],
        "fn": tool_batch_request_generate,
    },
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
    "list_assets": {
        "description": "查询项目生成的资产清单（图片/视频等）：类型、名称、版本、有效性、质量状态，可返回给前端展示缩略图",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListAssetsInput,
        "params": {
            "project_id": "项目 ID",
            "asset_type": "可选：资产类型过滤（keyframe/clip/character/scene/prop/reference）",
            "validity": "可选：有效性过滤（current/superseded/stale）",
            "limit": "可选：返回条数上限，默认 8，最大 30",
            "offset": "可选：偏移量（翻页用），默认 0",
        },
        "fn": tool_list_assets,
    },
    "list_asset_plans": {
        "description": "查询项目资产定义清单：类型/名称/状态/人工确认标记/依赖摘要（哪些定义还没生成、待确认）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListAssetPlansInput,
        "params": {
            "project_id": "项目 ID",
            "plan_type": "可选：定义类型过滤（character/scene/prop/keyframe/clip）",
            "status": "可选：状态过滤（pending/generated/reviewed/rejected）",
            "limit": "可选：返回条数上限，默认 20，最大 50",
            "offset": "可选：偏移量（翻页用），默认 0",
        },
        "fn": tool_list_asset_plans,
    },
    "request_generate": {
        "description": "提交生成申请（需人工审批）：只创建审批记录，不建任务、不消耗算力；镜头目标给 shot_id，公共资产目标（角色/场景/道具）给 entity_id",
        "permission": "generate",
        "risk_level": "medium",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RequestGenerateInput,
        "params": {
            "project_id": "项目 ID",
            "shot_id": "镜头 ID（镜头目标必填）",
            "entity_id": "实体 ID（角色/场景/道具目标必填）",
            "target": "keyframe/clip/audio/bgm/sfx/character/scene/prop",
            "prompt": "可选：追加提示词",
        },
        "preconditions": [_precheck_project_exists, _precheck_script_parsed],
        "fn": tool_request_generate,
    },
    "run_generate": {
        "description": "执行已批准的生成申请：创建生成任务并入队（须先通过人工审批）",
        "permission": "generate",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RunGenerateInput,
        "params": {"confirmation_id": "审批记录 ID"},
        "fn": tool_run_generate,
    },
    "get_style_config": {
        "description": "读取项目剧本类型与风格配置：题材、叙事风格、主视觉风格、节奏、情绪、受众及校验问题",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetStyleConfigInput,
        "params": {"project_id": "项目 ID（prj_ 前缀）"},
        "fn": tool_get_style_config,
    },
    "request_style_update": {
        "description": "提交项目风格配置变更申请（需人工审批）：只建审批记录，不改配置；只提交需要修改的字段",
        "permission": "write",
        "risk_level": "medium",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": UpdateStyleConfigInput,
        "params": {
            "session_id": "会话 ID（审批记录归属）",
            "project_id": "项目 ID",
            "visual_style": "可选：主视觉风格（单选）",
            "genre_tags": "可选：题材标签列表",
            "narrative_styles": "可选：叙事风格列表",
            "pacing": "可选：剧情节奏",
            "emotional_tone": "可选：情绪基调列表",
            "custom_style_requirements": "可选：自定义风格要求",
        },
        "preconditions": [_precheck_project_exists],
        "fn": tool_request_style_update,
    },
    "run_style_update": {
        "description": "执行已批准的风格配置变更：合并现有配置后规范化落库（须先通过人工审批）",
        "permission": "write",
        "risk_level": "high",
        "side_effect": True,
        "idempotent": True,
        "requires_approval": True,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": RunStyleUpdateInput,
        "params": {"confirmation_id": "审批记录 ID"},
        "fn": tool_run_style_update,
    },
    # --- 生成进度（只读：与资产库/分镜图/分镜看板页面同口径） ---
    "get_asset_generation_progress": {
        "description": (
            "查询公共资产图生成进度：角色定妆图/场景基准图/道具锚定图的 done/total、"
            "正在生成的条目与百分比、最近失败"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectProgressInput,
        "params": {
            "project_id": "项目 ID",
            "failed_hours": "可选：最近失败统计窗口（小时），默认 24",
        },
        "fn": tool_get_asset_generation_progress,
    },
    "get_keyframe_generation_progress": {
        "description": (
            "查询分镜图生成进度：全片镜头数、已出图、已人工确认、正在生成的镜头、最近失败"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectProgressInput,
        "params": {
            "project_id": "项目 ID",
            "failed_hours": "可选：最近失败统计窗口（小时），默认 24",
        },
        "fn": tool_get_keyframe_generation_progress,
    },
    "get_clip_generation_progress": {
        "description": (
            "查询分镜视频生成进度：已生成/总数、正在渲染的镜头、排队名次、"
            "剩余耗时估算（按近 7 日平均单镜耗时）、最近失败"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ProjectProgressInput,
        "params": {
            "project_id": "项目 ID",
            "failed_hours": "可选：最近失败统计窗口（小时），默认 24",
        },
        "fn": tool_get_clip_generation_progress,
    },
    # --- 任务诊断（只读） ---
    "get_job": {
        "description": "查询单个任务详情：状态、错误码与失败原因、重试次数、可重试性、产物与入参摘要",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetJobInput,
        "params": {"job_id": "任务 ID（job_ 前缀）"},
        "fn": tool_get_job,
    },
    # --- 实体与镜头定位（只读：把名称解析成 ID） ---
    "list_project_entities": {
        "description": (
            "列出项目角色/场景/道具的 ID 与名称（支持名称模糊匹配），并标注是否已有定妆图/基准图/锚定图；"
            "用于把用户说的名字解析成 chr_/scn_/prp_ 实体 ID，不要反问用户要 ID"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListProjectEntitiesInput,
        "params": {
            "project_id": "项目 ID",
            "query": "可选：名称或 ID 关键字（模糊匹配）",
            "entity_type": "可选：character / scene / prop",
            "limit": "可选：返回条数上限，默认 50",
        },
        "fn": tool_list_project_entities,
    },
    "list_project_shots": {
        "description": (
            "列出项目镜头：集数、场景、镜号、是否有分镜图/视频、分镜图是否已确认；"
            "按 episode + shot_seq 可定位『第 N 镜』，用于把镜号解析成 sht_ 镜头 ID"
        ),
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListProjectShotsInput,
        "params": {
            "project_id": "项目 ID",
            "episode": "可选：集数",
            "scene_id": "可选：场景 ID",
            "shot_seq": "可选：场景内镜号",
            "limit": "可选：返回条数上限，默认 60",
        },
        "fn": tool_list_project_shots,
    },
    # --- 解析 / 合成 / 导出 / 资产版本（只读） ---
    "get_parse_status": {
        "description": "查询剧本分析状态：是否已保存剧本、当前解析版本、各集解析进度、镜头与资产是否已产出",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetParseStatusInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_get_parse_status,
    },
    "list_compositions": {
        "description": "查询合成记录：集数、状态、是否过期、时长、告警（用于判断能否导出）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListCompositionsInput,
        "params": {"project_id": "项目 ID", "limit": "可选：返回条数上限，默认 10"},
        "fn": tool_list_compositions,
    },
    "list_exports": {
        "description": "查询导出记录：导出 ID、交付档位、状态、文件大小（导出 ID 可用于下载）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListExportsInput,
        "params": {"project_id": "项目 ID"},
        "fn": tool_list_exports,
    },
    "list_memories": {
        "description": "查询长期记忆：项目规范、用户偏好、历史经验（默认仅有效记忆）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListMemoriesInput,
        "params": {
            "project_id": "可选：项目 ID（prj_ 前缀）；不传时查全局偏好记忆",
            "memory_type": "可选：记忆类型过滤（project/preference/experience）",
            "limit": "可选：返回条数上限，默认 20",
        },
        "fn": tool_list_memories,
    },
    "add_memory": {
        "description": "写入一条长期记忆：用户明确要求记住的规范、偏好或经验（500 字内）",
        "permission": "write",
        "risk_level": "low",
        "side_effect": True,
        "idempotent": False,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": AddMemoryInput,
        "params": {
            "content": "记忆内容（简洁条目式，500 字内）",
            "memory_type": "记忆类型：project（项目规范）/ preference（用户偏好）/ experience（经验规则）",
            "project_id": "可选：关联项目 ID（preference 全局偏好可不传）",
        },
        "fn": tool_add_memory,
    },
    "supersede_memory": {
        "description": "替代一条旧记忆：旧记忆作废（保留可追溯），可同时写入新记忆",
        "permission": "write",
        "risk_level": "low",
        "side_effect": True,
        "idempotent": False,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": SupersedeMemoryInput,
        "params": {
            "memory_id": "待替代的记忆 ID（amr_ 前缀）",
            "new_content": "可选：新记忆内容；不传时仅作废旧记忆",
        },
        "fn": tool_supersede_memory,
    },
    "list_asset_versions": {
        "description": "查询资产版本历史：版本号、有效性、是否锁定、质量状态、来源任务（用于采纳候选/回滚决策）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": ListAssetVersionsInput,
        "params": {"asset_id": "资产 ID（ast_ 前缀）", "limit": "可选：返回条数上限，默认 20"},
        "fn": tool_list_asset_versions,
    },
    "get_shot_impact": {
        "description": "查询镜头改动影响面：该镜生效产物、是否锁定、是否需前序尾帧、确认状态（重生成前预判）",
        "permission": "read",
        "risk_level": "low",
        "side_effect": False,
        "idempotent": True,
        "requires_approval": False,
        "timeout_seconds": 30,
        "max_retries": 0,
        "input_model": GetShotImpactInput,
        "params": {"shot_id": "镜头 ID（sht_ 前缀）"},
        "fn": tool_get_shot_impact,
    },
}

# 意图 → 候选工具映射（只向模型暴露当前意图需要的工具，降低选错/越权/Token 消耗）
# 全部为只读工具：查询/分析意图不暴露任何生成或写操作工具
_READ_ONLY_TOOLS = [
    "get_shot_prompt",
    "script_normalize",
    "script_evaluate",
    "project_overview",
    "list_jobs",
    "get_job",
    "asset_stats",
    "list_assets",
    "list_asset_versions",
    "list_asset_plans",
    "list_project_entities",
    "list_project_shots",
    "get_shot_impact",
    "get_asset_generation_progress",
    "get_keyframe_generation_progress",
    "get_clip_generation_progress",
    "get_parse_status",
    "list_compositions",
    "list_exports",
    "get_style_config",
]

INTENT_TOOL_MAPPING: dict[str, list[str]] = {
    "query": list(_READ_ONLY_TOOLS),
    "analysis": list(_READ_ONLY_TOOLS),
    # 生成请求定位不到目标时回落主对话：放开只读工具，
    # 让大模型兜底时能拿到实体/镜头清单作为可选项（规则不兜底）
    "generate_request": list(_READ_ONLY_TOOLS),
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
            "preconditions": [
                getattr(fn, "__name__", str(fn))
                for fn in (spec.get("preconditions") or [])
            ],
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
    # 策略引擎（agent_governance）：审批/高风险门禁由确定性代码判定，不依赖 Prompt 约束
    from app.services.agent_governance import evaluate_tool_policy

    decision = evaluate_tool_policy(spec, approved=approved)
    if decision != "ALLOW":
        return ToolResult(
            ok=False,
            error_code="approval_required",
            error_message=f"工具 {name} 需要人工审批后执行",
            data={"tool": name, "params": kwargs, "permission": spec.get("permission")},
        )
    # 前置条件检查（Phase 2）：按注册表 preconditions 列表顺序执行，首个失败即阻断
    preconditions: list = spec.get("preconditions") or []
    for check in preconditions:
        ok_flag, msg = check(db, **kwargs)
        if not ok_flag:
            return ToolResult(
                ok=False,
                error_code="precondition_failed",
                error_message=msg or f"工具 {name} 前置条件未满足",
                data={
                    "tool": name,
                    "precondition_code": getattr(check, "__name__", "unknown"),
                },
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
