"""模型包入口：集中导入全部实体，供 Alembic 收集元数据与应用层使用。"""
from app.models.base import Base, TimestampMixin
from app.models.narrative import (
    Character,
    EpisodeParseRecord,
    Project,
    Prop,
    Scene,
    SceneEpisode,
    ScriptEstimate,
    ScriptVersion,
    Shot,
)
from app.models.production import (
    Asset,
    AssetRef,
    Composition,
    DependencyRef,
    Export,
    Job,
    JobEvent,
    ParseVersion,
    UploadRecord,
    Workflow,
)
from app.models.continuity import EpisodeContinuity
from app.models.agent import (
    AgentConfirmation,
    AgentMessage,
    AgentPlanStep,
    AgentSession,
    AgentTask,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "Character",
    "EpisodeParseRecord",
    "Project",
    "Prop",
    "Scene",
    "SceneEpisode",
    "ScriptEstimate",
    "ScriptVersion",
    "Shot",
    "Asset",
    "AssetRef",
    "Composition",
    "DependencyRef",
    "Export",
    "Job",
    "JobEvent",
    "ParseVersion",
    "UploadRecord",
    "Workflow",
    "EpisodeContinuity",
    "AgentSession",
    "AgentMessage",
    "AgentTask",
    "AgentPlanStep",
    "AgentConfirmation",
]
