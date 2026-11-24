"""场景与镜头域校验模式：API.md §5 剧本与分镜中场景/镜头接口契约。"""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

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
    # --- 聚合改造新增 ---
    timeline: list | None = None
    cut_reason: str | None = None
    aggregation_meta: dict | None = None


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
    # --- 聚合改造新增（P1 数据层）---
    timeline: list | None = None
    cut_reason: str | None = None
    aggregation_meta: dict | None = None
    # --- 镜头级契约（shot_plan v1，分镜图生成改造 Phase 1）---
    shot_plan: dict | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StoryboardPlanValidate(BaseModel):
    """分镜规划校验请求：结构由服务层逐项验证，返回问题与 dry-run 预览。"""

    plan: dict


# ---------------------------------------------------------------------------
# 镜头级契约 shot_plan（schema v1，分镜图生成改造 Phase 1）
#
# 分镜规划的导演决策落库结构：画内角色 / 画外声音 / 可见道具 / 构图 / 首帧状态。
# 下游 compiler / resolver / 门禁 / 注入器统一读这一份出场清单（ShotCast），
# 不再各写一套启发式推断「谁在画面里」。资产 ID 存在性、归属项目、
# 说话人归类等需要查库的校验在服务层（storyboard_plan.py）完成。
# ---------------------------------------------------------------------------

SHOT_PLAN_ROLES = ("primary", "secondary", "background")
SHOT_PLAN_OFFSCREEN_KINDS = ("voice_over", "offscreen_dialogue", "mentioned")
SHOT_PLAN_PROP_INTERACTIONS = ("held", "carried", "worn", "placed", "none")
SHOT_PLAN_SOURCES = ("storyboard_plan", "parse_heuristic", "manual")


class ShotPlanOnScreenCharacter(BaseModel):
    """画内角色：谁在画面里、站在哪、朝向、看谁、首帧动作。"""

    character_id: str = Field(min_length=1)
    role: str = "primary"
    position: dict | None = None
    posture: str | None = None
    facing: str | None = None
    gaze_target: dict | None = None
    action_at_start: str | None = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in SHOT_PLAN_ROLES:
            raise ValueError(f"role 须为 {SHOT_PLAN_ROLES} 之一")
        return v


class ShotPlanOffscreenVoice(BaseModel):
    """画外声音：有台词/被提及但不在画面里的角色。"""

    character_id: str = Field(min_length=1)
    kind: str = "voice_over"

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in SHOT_PLAN_OFFSCREEN_KINDS:
            raise ValueError(f"kind 须为 {SHOT_PLAN_OFFSCREEN_KINDS} 之一")
        return v


class ShotPlanVisibleProp(BaseModel):
    """可见道具：本镜画面里出现的道具及其持有人/交互方式/位置。"""

    prop_id: str = Field(min_length=1)
    holder_character_id: str | None = None
    interaction: str = "none"
    position: str | None = None
    appearance_constraint: str | None = None

    @field_validator("interaction")
    @classmethod
    def _check_interaction(cls, v: str) -> str:
        if v not in SHOT_PLAN_PROP_INTERACTIONS:
            raise ValueError(f"interaction 须为 {SHOT_PLAN_PROP_INTERACTIONS} 之一")
        return v


class ShotPlanComposition(BaseModel):
    """构图决策：景别/机位/取景/焦点/关系（谁递给谁、谁看谁）。"""

    shot_size: str | None = None
    camera_angle: str | None = None
    framing: str | None = None
    focus: str | None = None
    relation_note: str | None = None


class ShotPlanKeyframe(BaseModel):
    """首帧状态：作为图生视频首帧的完整可定格瞬间。"""

    t: int = 0
    state: str | None = None
    avoid: list[str] = []


class ShotPlan(BaseModel):
    """镜头级契约（Shot.shot_plan 列，schema v1）。"""

    version: int = 1
    source: str = "storyboard_plan"
    confirmed: bool = False
    visual_description: str | None = None
    on_screen_characters: list[ShotPlanOnScreenCharacter] = []
    offscreen_voices: list[ShotPlanOffscreenVoice] = []
    visible_props: list[ShotPlanVisibleProp] = []
    composition: ShotPlanComposition | None = None
    keyframe: ShotPlanKeyframe | None = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if v not in SHOT_PLAN_SOURCES:
            raise ValueError(f"source 须为 {SHOT_PLAN_SOURCES} 之一")
        return v

    @field_validator("on_screen_characters")
    @classmethod
    def _check_unique_on_screen(cls, v: list[ShotPlanOnScreenCharacter]) -> list[ShotPlanOnScreenCharacter]:
        seen = [item.character_id for item in v]
        if len(seen) != len(set(seen)):
            raise ValueError("on_screen_characters 中 character_id 重复")
        return v

    @field_validator("offscreen_voices")
    @classmethod
    def _check_unique_offscreen(cls, v: list[ShotPlanOffscreenVoice]) -> list[ShotPlanOffscreenVoice]:
        seen = [item.character_id for item in v]
        if len(seen) != len(set(seen)):
            raise ValueError("offscreen_voices 中 character_id 重复")
        return v

    @field_validator("visible_props")
    @classmethod
    def _check_unique_props(cls, v: list[ShotPlanVisibleProp]) -> list[ShotPlanVisibleProp]:
        seen = [item.prop_id for item in v]
        if len(seen) != len(set(seen)):
            raise ValueError("visible_props 中 prop_id 重复")
        return v

    @model_validator(mode="after")
    def _check_exclusive(self) -> "ShotPlan":
        on_screen = {item.character_id for item in self.on_screen_characters}
        offscreen = {item.character_id for item in self.offscreen_voices}
        overlap = on_screen & offscreen
        if overlap:
            raise ValueError(
                f"同一角色不能同时画内画外：{sorted(overlap)}"
            )
        return self
