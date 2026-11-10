"""系列化创作域校验模式：字段级契约与状态枚举校验。

约定：
- 所有输入使用 Field 约束长度与范围，禁止无限制透传。
- 状态字段只能取既有枚举集合，非法值直接 422。
- 输出模型统一 from_attributes，便于由 ORM 对象直接序列化。
"""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.series import EPISODE_CONTINUITY_STATUS, SCRIPT_STATUS, SERIES_STATUS
from app.models.series_story import (
    PLOT_THREAD_STATUS,
    STORY_EVENT_FACT_STATUS,
    STORY_EVENT_TYPES,
)


# --- Series ---


class SeriesCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    cover_image: str | None = Field(default=None, max_length=500)
    genre: str | None = Field(default=None, max_length=64)
    style: str | None = Field(default=None, max_length=200)
    target_audience: str | None = Field(default=None, max_length=64)
    language: str = Field(default="zh-CN", max_length=16)
    bible: dict | None = None


class SeriesUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    cover_image: str | None = Field(default=None, max_length=500)
    genre: str | None = Field(default=None, max_length=64)
    style: str | None = Field(default=None, max_length=200)
    target_audience: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("name 不能为空白")
        return v


class SeriesOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    cover_image: str | None = None
    genre: str | None = None
    style: str | None = None
    target_audience: str | None = None
    language: str
    status: str
    mainline_id: str
    latest_confirmed_episode_id: str | None = None
    latest_completed_episode_id: str | None = None
    state_version: int
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class SeriesListOut(SeriesOut):
    """列表项：附带派生统计，字段均有真实来源，缺失时为 0 而非伪造。"""

    episode_count: int = 0
    confirmed_script_count: int = 0
    completed_video_count: int = 0
    open_plot_thread_count: int = 0
    bible_version: int = 0


class SeriesPageOut(BaseModel):
    items: list[SeriesListOut]
    total: int
    page: int
    page_size: int


# --- Bible ---


class BibleSaveIn(BaseModel):
    content: dict
    change_summary: str | None = Field(default=None, max_length=1000)


class BibleOut(BaseModel):
    id: str
    series_id: str
    version: int
    content: dict | None = None
    change_summary: str | None = None
    created_by: str | None = None
    created_at: datetime
    is_active: bool
    model_config = {"from_attributes": True}


# --- Episode ---


class EpisodeCreate(BaseModel):
    episode_number: int | None = Field(default=None, ge=1, le=9999)
    title: str | None = Field(default=None, max_length=200)
    synopsis: str | None = Field(default=None, max_length=4000)
    project_id: str | None = Field(default=None, max_length=32)


class EpisodeOut(BaseModel):
    id: str
    series_id: str
    project_id: str | None = None
    project_episode_no: int | None = None
    episode_number: int
    title: str | None = None
    synopsis: str | None = None
    script_status: str
    production_status: str
    script_current_version_id: str | None = None
    confirmed_script_version_id: str | None = None
    context_manifest_id: str | None = None
    continuity_status: str
    mainline_id: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}

    @field_validator("script_status")
    @classmethod
    def _check_script_status(cls, v: str) -> str:
        if v not in SCRIPT_STATUS:
            raise ValueError(f"script_status 须为 {SCRIPT_STATUS} 之一")
        return v

    @field_validator("continuity_status")
    @classmethod
    def _check_continuity_status(cls, v: str) -> str:
        if v not in EPISODE_CONTINUITY_STATUS:
            raise ValueError(f"continuity_status 须为 {EPISODE_CONTINUITY_STATUS} 之一")
        return v


class EpisodePageOut(BaseModel):
    items: list[EpisodeOut]
    total: int
    page: int
    page_size: int


# --- Script version ---


class ScriptSaveIn(BaseModel):
    content: str = Field(min_length=1)
    synopsis: str | None = Field(default=None, max_length=4000)
    source_context_manifest_id: str | None = Field(default=None, max_length=32)
    prompt_template_version: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=64)


class ScriptOut(BaseModel):
    id: str
    episode_id: str
    version: int
    content: str | None = None
    synopsis: str | None = None
    source_context_manifest_id: str | None = None
    prompt_template_version: str | None = None
    model: str | None = None
    validation_result: dict | None = None
    continuity_result: dict | None = None
    status: str
    created_at: datetime
    created_by: str | None = None
    model_config = {"from_attributes": True}


class ScriptConfirmIn(BaseModel):
    """确认剧本：必须携带提交方看到的系列状态版本，用于乐观锁校验。"""

    expected_series_state_version: int = Field(ge=1)
    review_note: str | None = Field(default=None, max_length=1000)
    ignore_warnings: bool = False


class ScriptConfirmOut(BaseModel):
    episode_id: str
    script_version_id: str
    script_status: str
    series_state_version: int
    snapshot_version: int
    committed_event_count: int
    issues: list[dict] = []
    ignored_issue_ids: list[str] = []


# --- Context ---


class ContextManifestOut(BaseModel):
    id: str
    series_id: str
    episode_id: str
    context_version: int
    manifest_json: dict | None = None
    context_hash: str | None = None
    prompt_template_version: str | None = None
    model: str | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


# --- Story events ---


class StoryEventOut(BaseModel):
    id: str
    series_id: str
    episode_id: str
    script_version_id: str
    event_key: str
    event_type: str
    title: str
    description: str | None = None
    involved_asset_ids: list | None = None
    location_asset_id: str | None = None
    source_scene_id: str | None = None
    source_text: str | None = None
    fact_status: str
    importance: int
    occurred_order: int | None = None
    created_at: datetime
    model_config = {"from_attributes": True}

    @field_validator("event_type")
    @classmethod
    def _check_event_type(cls, v: str) -> str:
        if v not in STORY_EVENT_TYPES:
            raise ValueError(f"event_type 须为 {STORY_EVENT_TYPES} 之一")
        return v

    @field_validator("fact_status")
    @classmethod
    def _check_fact_status(cls, v: str) -> str:
        if v not in STORY_EVENT_FACT_STATUS:
            raise ValueError(f"fact_status 须为 {STORY_EVENT_FACT_STATUS} 之一")
        return v


# --- Snapshot ---


class ContinuitySnapshotOut(BaseModel):
    id: str
    series_id: str
    mainline_id: str
    version: int
    based_on_episode_id: str | None = None
    based_on_script_version_id: str | None = None
    state_json: dict | None = None
    state_hash: str | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


class ContinuityOverviewOut(BaseModel):
    series_id: str
    state_version: int
    latest_snapshot: ContinuitySnapshotOut | None = None
    open_plot_threads: int = 0
    confirmed_event_count: int = 0
    episodes_with_conflict: int = 0


# --- Plot threads ---


class PlotThreadCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    introduced_episode_id: str | None = Field(default=None, max_length=32)
    related_asset_ids: list[str] | None = None
    resolution_condition: str | None = Field(default=None, max_length=2000)


class PlotThreadUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    status: str | None = None
    last_updated_episode_id: str | None = Field(default=None, max_length=32)
    resolution_condition: str | None = Field(default=None, max_length=2000)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str | None) -> str | None:
        if v is not None and v not in PLOT_THREAD_STATUS:
            raise ValueError(f"status 须为 {PLOT_THREAD_STATUS} 之一")
        return v


class PlotThreadOut(BaseModel):
    id: str
    series_id: str
    title: str
    description: str | None = None
    introduced_episode_id: str | None = None
    last_updated_episode_id: str | None = None
    status: str
    related_asset_ids: list | None = None
    related_event_ids: list | None = None
    resolution_condition: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    model_config = {"from_attributes": True}


# --- Continue (异步续写) ---


class ContinueConstraints(BaseModel):
    reuse_assets: bool = True
    preserve_character_identity: bool = True
    avoid_story_conflicts: bool = True


class ContinueIn(BaseModel):
    user_instruction: str = Field(min_length=1, max_length=4000)
    direction: str = Field(default="continue", max_length=32)
    constraints: ContinueConstraints = Field(default_factory=ContinueConstraints)
    target_episode_number: int | None = Field(default=None, ge=1, le=9999)
    title: str | None = Field(default=None, max_length=200)

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, v: str) -> str:
        allowed = ("continue", "advance_thread", "new_event", "custom")
        if v not in allowed:
            raise ValueError(f"direction 须为 {allowed} 之一")
        return v


class ContinueOut(BaseModel):
    episode_id: str
    episode_number: int
    task_id: str
    status: str


# --- Asset registry ---


class SeriesAssetOut(BaseModel):
    id: str
    series_id: str
    asset_id: str
    first_episode_id: str | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


class EpisodeAssetRefOut(BaseModel):
    id: str
    episode_id: str
    asset_id: str
    asset_version: int
    usage_type: str
    is_required: bool
    source: str | None = None
    created_at: datetime
    model_config = {"from_attributes": True}


class SeriesStatusPatch(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in SERIES_STATUS:
            raise ValueError(f"status 须为 {SERIES_STATUS} 之一")
        return v
