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
    JobInputRef,
    ParseVersion,
    UploadRecord,
    Workflow,
)
from app.models.asset_plan import AssetPlan, AssetPlanDependency
from app.models.continuity import EpisodeContinuity
from app.models.prop_lifecycle import PropEpisodeSnapshot, PropLifecycleEvent, PropShotState
from app.models.prop_binding import CharacterPropBinding
from app.models.agent import (
    AgentConfirmation,
    AgentMemory,
    AgentMessage,
    AgentPlanStep,
    AgentSession,
    AgentTask,
)
from app.models.task import (
    Task,
    TaskEvent,
    TaskResult,
    TaskStage,
)
from app.models.search import SearchRun, SearchResult
from app.models.audio import AudioReview, AudioSegment, VoiceLibrary, VoiceProfile
from app.models.series import (
    Series,
    SeriesBibleVersion,
    SeriesEpisode,
    SeriesScriptVersion,
)
from app.models.series_asset import EpisodeAssetRef, SeriesAsset
from app.models.series_story import (
    ContinuityChangeProposal,
    ContinuitySnapshot,
    EpisodeContextManifest,
    PlotThread,
    StoryEvent,
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
    "JobInputRef",
    "ParseVersion",
    "UploadRecord",
    "Workflow",
    "EpisodeContinuity",
    "AssetPlan",
    "AssetPlanDependency",
    "PropLifecycleEvent",
    "PropShotState",
    "PropEpisodeSnapshot",
    "CharacterPropBinding",
    "AgentSession",
    "AgentMessage",
    "AgentTask",
    "AgentPlanStep",
    "SearchRun",
    "SearchResult",
    "VoiceProfile",
    "VoiceLibrary",
    "AudioSegment",
    "AudioReview",
    "AgentConfirmation",
    "AgentMemory",
    "Task",
    "TaskStage",
    "TaskEvent",
    "TaskResult",
    "Series",
    "SeriesEpisode",
    "SeriesBibleVersion",
    "SeriesScriptVersion",
    "SeriesAsset",
    "EpisodeAssetRef",
    "StoryEvent",
    "ContinuitySnapshot",
    "ContinuityChangeProposal",
    "EpisodeContextManifest",
    "PlotThread",
]
