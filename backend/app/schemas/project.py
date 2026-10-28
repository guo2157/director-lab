"""项目域校验模式：API.md §4 项目管理字段级契约。"""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import ASPECT_RATIO, CREATION_TYPE


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    aspect_ratio: str
    creation_type: str = "novel"
    description: str | None = Field(default=None, max_length=500)

    @field_validator("aspect_ratio")
    @classmethod
    def _check_ratio(cls, v: str) -> str:
        if v not in ASPECT_RATIO:
            raise ValueError(f"aspect_ratio 须为 {ASPECT_RATIO} 之一")
        return v

    @field_validator("creation_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in CREATION_TYPE:
            raise ValueError(f"creation_type 须为 {CREATION_TYPE} 之一")
        return v


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    settings: dict | None = None

    @field_validator("settings")
    @classmethod
    def _check_settings(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        p = v.get("consistency_pass_threshold")
        f = v.get("consistency_fail_threshold")
        if p is not None and f is not None and not (f < p):
            raise ValueError("consistency_fail_threshold 须小于 consistency_pass_threshold")
        return v


class ProjectOut(BaseModel):
    id: str
    name: str
    creation_type: str
    description: str | None
    aspect_ratio: str
    status: str
    settings: dict | None
    episode_plan: list | None
    estimated_at: datetime | None
    estimated_text_hash: str | None
    current_script_version_id: str | None
    current_parse_version_id: str | None
    # 公共资产人工审核状态（D-23）：pending / approved / rejected
    assets_review_status: str = "pending"
    assets_reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    shot_total: int = 0
    shot_done: int = 0
    current_stage: str = "estimate"

    model_config = {"from_attributes": True}


class PageOut(BaseModel):
    items: list
    total: int
    page: int
    page_size: int
