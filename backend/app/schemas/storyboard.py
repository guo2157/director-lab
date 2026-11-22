"""场景与镜头域校验模式：API.md §5 剧本与分镜中场景/镜头接口契约。"""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import SHOT_KIND


class SceneCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    episode: int = Field(default=1, ge=1)
    seq: int | None = Field(default=None, ge=1)


class SceneUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    time_note: str | None = None


class SceneEpisodeAdd(BaseModel):
    episode: int = Field(ge=1)
    seq_in_episode: int = Field(ge=1)


class SceneOut(BaseModel):
    id: str
    project_id: str
    episode: int
    seq: int
    title: str
    description: str | None
    time_note: str | None
    baseline_asset_id: str | None
    episodes: list[int] = []
    shot_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SceneEpisodeAddOut(BaseModel):
    scene: SceneOut
    requires_parse: bool


class ShotCreate(BaseModel):
    scene_id: str
    episode: int = Field(ge=1)
    shot_kind: str
    dialogue: str | None = None
    image_prompt: str | None = None
    video_prompt: str | None = None
    duration: int = Field(ge=1)
    character_id: str | None = None
    # 尾帧依赖声明（P0）：为 True 时生成 clip 必须有上一镜可用尾帧，否则 409
    requires_prev_frame: bool = False

    @field_validator("shot_kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in SHOT_KIND:
            raise ValueError(f"shot_kind 须为 {SHOT_KIND} 之一")
        return v


class ShotUpdate(BaseModel):
    dialogue: str | None = None
    image_prompt: str | None = None
    video_prompt: str | None = None
    duration: int | None = Field(default=None, ge=1)
    shot_type: str | None = None
    camera_angle: str | None = None
    camera_movement: str | None = None
    transition: str | None = None
    shot_purpose: str | None = None
    emotion: str | None = None
    soundscape: str | None = None
    bgm: str | None = None
    character_id: str | None = None
    # 尾帧依赖声明（P0）：可随时开关，变更会经由既有失效传播标记下游
    requires_prev_frame: bool | None = None


class ShotOrderUpdate(BaseModel):
    episode: int = Field(ge=1)
    shot_ids: list[str]


class ShotOut(BaseModel):
    id: str
    scene_id: str
    episode: int
    character_id: str | None
    seq: int
    dialogue: str | None
    dialogue_segments: list | None
    shot_kind: str
    image_prompt: str | None
    video_prompt: str | None
    shot_type: str | None
    camera_angle: str | None
    camera_movement: str | None
    transition: str | None
    shot_purpose: str | None
    emotion: str | None
    soundscape: str | None
    bgm: str | None
    duration: int
    status: str
    keyframe_confirmed: bool = False
    actual_duration: float | None
    video_actual_duration: float | None
    audio_source: str | None
    audio_check_status: str | None
    dialogue_check_status: str | None
    av_sync_delta: float | None
    time_jump: bool
    requires_prev_frame: bool
    start_state: dict | None
    end_state: dict | None
    last_frame_asset_id: str | None
    beats: list | None
    emotion_curve: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StoryboardPlanValidate(BaseModel):
    """分镜规划校验请求：结构由服务层逐项验证，返回问题与 dry-run 预览。"""

    plan: dict
