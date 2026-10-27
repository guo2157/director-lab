"""剧本与任务接口的数据结构：API.md §5 剧本与分镜、§任务 契约。"""
from datetime import datetime

from pydantic import BaseModel, Field


# --- 剧本版本 ---
class ScriptVersionCreate(BaseModel):
    text: str = Field(min_length=1, max_length=50000)


class ScriptVersionOut(BaseModel):
    script_version_id: str
    version: int
    content_hash: str


# --- 评估 ---
class EstimateCreate(BaseModel):
    script_version_id: str
    text: str = Field(min_length=1, max_length=50000)


class EpisodeBreakdown(BaseModel):
    episode: int
    estimated_duration: float
    estimated_shots: int


class EstimateOut(BaseModel):
    estimate_id: str
    script_version_id: str
    algorithm_version: str
    char_count: int
    dialogue_char_estimate: int
    estimated_total_duration: float
    estimated_episodes: int
    episode_breakdown: list[EpisodeBreakdown]
    exceeds_batch_limit: bool
    suggestion: str | None
    user_confirmed: bool


class EstimateConfirm(BaseModel):
    script_version_id: str
    episode_start: int = Field(ge=1)
    episode_count: int = Field(ge=1)
    duration_adjust: float | None = None


class EstimateConfirmOut(BaseModel):
    estimate_id: str
    user_confirmed: bool
    episode_start: int
    episode_count: int
    # 确认后锁定的总集数：此后解析范围不可超出
    total_episodes: int | None = None
    locked: bool = False


# --- 解析 ---
class ParseCreate(BaseModel):
    script_version_id: str
    text: str = Field(min_length=1, max_length=50000)
    episode_start: int = Field(ge=1)
    # 两种输入模式二选一：episode_count（起始+数量）或 episode_end（起止集数），后端统一归一化为区间
    episode_count: int | None = Field(default=None, ge=1)
    episode_end: int | None = Field(default=None, ge=1)


class ParseJobOut(BaseModel):
    job_id: str
    status: str


# --- 集级解析状态（集数网格视图） ---
class EpisodeParseStatusItem(BaseModel):
    episode_no: int
    asset_type: str
    status: str
    attempt_count: int
    error_msg: str | None
    asset_count: int | None
    parse_version_id: str | None
    updated_at: datetime | None


class ParseStatusOut(BaseModel):
    total_episodes: int | None
    records: list[EpisodeParseStatusItem]


class ParseRetryOut(BaseModel):
    job_id: str
    retried_episodes: list[int]


class ParseVersionConfirm(BaseModel):
    current_parse_version_id: str | None = None  # 乐观锁：请求方所见的当前版本


class ParseVersionOut(BaseModel):
    parse_version_id: str
    version: int
    status: str
    diff_summary: dict | None


# --- 合成 ---
class TimelineTrack(BaseModel):
    role: str
    asset_id: str
    asset_version: int | None = None
    shot_id: str | None = None
    scene_id: str | None = None
    episode: int | None = None
    start: float | None = None
    end: float | None = None
    volume: float | None = None
    fade_in: float | None = None
    fade_out: float | None = None
    loop_policy: str | None = None


class ComposeCreate(BaseModel):
    episode: int | None = Field(default=None, ge=1)
    include_subtitle: bool = True
    subtitle_mode: str = Field(default="burn", pattern="^(burn|sidecar)$")
    bgm_asset_id: str | None = None
    bgm_volume: float = Field(default=0.3, ge=0, le=1)
    compose_tolerance: float = Field(default=0.5, ge=0, le=2)
    timeline_tracks: list[TimelineTrack] | None = None


class CompositionOut(BaseModel):
    composition_id: str
    episode: int | None
    duration: float | None
    stale: bool
    created_at: datetime | None
    file_path: str | None
    status: str


class CompositionDetailOut(CompositionOut):
    source_snapshot: dict | None
    warnings: list | None
    job_id: str | None = None
    srt_path: str | None = None


# --- 导出 ---
class ExportCreate(BaseModel):
    composition_id: str
    format: str = Field(default="mp4", pattern="^mp4$")
    include_subtitle: bool | None = None
    subtitle_mode: str | None = None
    delivery_profile: str = Field(default="standard", pattern="^(standard|high_quality)$")


class ExportOut(BaseModel):
    id: str
    composition_id: str
    file_name: str | None
    size: int | None
    status: str
    created_at: datetime | None
    download_url: str


# --- 任务 ---
class JobOut(BaseModel):
    id: str
    project_id: str
    job_type: str
    status: str
    # 展示用关联信息（列表/详情接口补充，便于用户识别任务归属）
    project_name: str | None = None
    episode: int | None = None
    scene_title: str | None = None
    shot_seq: int | None = None
    error_code: str | None
    error_msg: str | None
    retryable: bool
    result: dict | None
    attempts: int
    seq: int | None = None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
