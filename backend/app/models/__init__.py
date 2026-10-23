"""模型包入口：集中导入全部实体，供 Alembic 收集元数据与应用层使用。"""
from app.models.base import Base, TimestampMixin
from app.models.narrative import (
    Character,
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

__all__ = [
    "Base",
    "TimestampMixin",
    "Character",
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
]
