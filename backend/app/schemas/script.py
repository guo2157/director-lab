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


# --- 解析 ---
class ParseCreate(BaseModel):
    script_version_id: str
    text: str = Field(min_length=1, max_length=50000)
    episode_start: int = Field(ge=1)
    episode_count: int = Field(ge=1)


class ParseJobOut(BaseModel):
    job_id: str
    status: str


class ParseVersionConfirm(BaseModel):
    current_parse_version_id: str | None = None  # 乐观锁：请求方所见的当前版本


class ParseVersionOut(BaseModel):
    parse_version_id: str
    version: int
    status: str
    diff_summary: dict | None


# --- 任务 ---
class JobOut(BaseModel):
    id: str
    project_id: str
    job_type: str
    status: str
    error_code: str | None
    error_msg: str | None
    retryable: bool
    result: dict | None
    attempts: int
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
