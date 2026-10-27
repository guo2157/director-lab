"""Agent 会话域校验模式：导演台会话与消息接口契约。"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 消息角色白名单（与 models/agent.py 注释一致）
AGENT_MESSAGE_ROLES = {"user", "assistant", "system", "tool_event", "approval"}


class AgentSessionCreate(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=100)


class AgentSessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=100)


class AgentSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    project_id: str | None
    status: str
    model: str | None
    created_at: datetime
    updated_at: datetime
    # 派生字段：消息数与最后一条消息时间（列表展示用）
    message_count: int = 0
    last_message_at: datetime | None = None


class AgentMessageCreate(BaseModel):
    role: str = Field(min_length=1, max_length=16)
    content: str = Field(default="", max_length=100_000)
    payload: dict | None = None
    attachment_id: str | None = Field(default=None, max_length=32)

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in AGENT_MESSAGE_ROLES:
            raise ValueError(f"role 须为 {sorted(AGENT_MESSAGE_ROLES)} 之一")
        return v


class AgentMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    payload: dict | None
    attachment_id: str | None
    seq: int
    created_at: datetime
