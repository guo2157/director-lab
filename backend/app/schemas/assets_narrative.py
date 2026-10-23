"""角色与道具域校验模式：API.md §5 剧本与分镜中角色/道具接口契约。"""
from datetime import datetime

from pydantic import BaseModel, Field


class CharacterCreate(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    traits: str | None = None
    appearance: str | None = None
    voice_id: str | None = None


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    traits: str | None = None
    appearance: str | None = None
    voice_id: str | None = None


class CharacterMerge(BaseModel):
    source_id: str
    target_id: str


class CharacterOut(BaseModel):
    id: str
    project_id: str
    name: str
    description: str | None
    traits: str | None
    appearance: str | None
    voice_id: str | None
    portrait_asset_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PropCreate(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    appearance: str | None = None


class PropUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    appearance: str | None = None


class PropOut(BaseModel):
    id: str
    project_id: str
    name: str
    description: str | None
    appearance: str | None
    is_key_prop: bool
    anchor_required: bool
    anchor_asset_id: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
