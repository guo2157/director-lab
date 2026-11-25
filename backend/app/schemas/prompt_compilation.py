"""阶段 B 提示词编译结果 Schema（严格模式）。

作用：LLM 或模板编译输出必须通过本 Schema 才允许提交生成任务，
非 JSON、缺字段、槽位结构错误一律拒绝，绝不静默丢弃。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TASK_TYPES = ("asset_image", "storyboard_image", "shot_video")
ASSET_TYPES = ("character", "scene", "prop", "keyframe", "clip")

# 任务类型与资产类型的合法组合，以及各组合必须使用的模板标识。
# 用于拦截『视频任务配角色资产』『分镜任务套角色模板』这类语义不可能成立的编译结果。
TEMPLATE_FOR_TASK_ASSET: dict[tuple[str, str], str] = {
    ("asset_image", "character"): "character_asset_prompt",
    ("asset_image", "scene"): "scene_asset_prompt",
    ("asset_image", "prop"): "prop_asset_prompt",
    ("storyboard_image", "keyframe"): "storyboard_image_prompt",
    ("shot_video", "clip"): "shot_video_prompt",
}


class ReferenceSlotModel(BaseModel):
    """参考图槽位：slot_key 必须与工作流 capabilities.reference_slots 的 param 对齐。"""

    model_config = ConfigDict(extra="forbid")

    slot_key: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=64)
    entity_type: str = Field(min_length=1, max_length=32)
    approved_asset_file_id: str = Field(min_length=1, max_length=64)
    file_path: str = Field(min_length=1, max_length=1024)
    role: str = Field(min_length=1, max_length=64)
    required: bool = False
    input_key: str | None = None


class BlockingIssueModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class CompiledPromptModel(BaseModel):
    """编译结果强校验：正向提示词必填非空，槽位唯一，状态与阻断项一致。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "needs_review"]
    task_type: Literal["asset_image", "storyboard_image", "shot_video"]
    asset_type: Literal["character", "scene", "prop", "keyframe", "clip"] | None = None
    entity_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=64)
    template_version: int = Field(ge=1)
    content_hash: str = Field(default="", max_length=128)
    positive_prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str = Field(default="", max_length=8000)
    reference_slots: list[ReferenceSlotModel] = Field(default_factory=list)
    generation_constraints: dict[str, Any] = Field(default_factory=dict)
    blocking_issues: list[BlockingIssueModel] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    canonical_snapshot: dict[str, Any] = Field(default_factory=dict)

    @field_validator("positive_prompt")
    @classmethod
    def _positive_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("positive_prompt_blank: 正向提示词不能为空白")
        return value

    @field_validator("reference_slots")
    @classmethod
    def _unique_slots(cls, value: list[ReferenceSlotModel]) -> list[ReferenceSlotModel]:
        keys = [slot.slot_key for slot in value]
        if len(keys) != len(set(keys)):
            raise ValueError("reference_slot_duplicated: 参考图槽位名必须唯一")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> CompiledPromptModel:
        """交叉校验：状态与阻断项一致、任务类型/资产类型/模板标识三者匹配。"""
        if self.status == "needs_review" and not self.blocking_issues:
            raise ValueError(
                "status_needs_review_requires_blocking: 状态为 needs_review 时必须给出阻断项"
            )
        expected = TEMPLATE_FOR_TASK_ASSET.get((self.task_type, self.asset_type))
        if expected is None:
            raise ValueError(
                f"task_asset_type_mismatch: {self.task_type}/{self.asset_type} 不是合法组合"
            )
        if self.template_id != expected:
            raise ValueError(
                f"template_task_mismatch: {self.task_type}/{self.asset_type} "
                f"应使用 {expected}，实际 {self.template_id}"
            )
        return self

    def is_ready(self) -> bool:
        return self.status == "ready" and not self.blocking_issues
