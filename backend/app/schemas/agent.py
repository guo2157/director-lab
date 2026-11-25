"""Agent 会话域校验模式：导演台会话与消息接口契约。"""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 消息角色白名单（与 models/agent.py 注释一致）
AGENT_MESSAGE_ROLES = {"user", "assistant", "system", "tool_event", "approval"}
# P0-4 API 输入安全（2026-11-25）：外部 append_message 接口允许的角色白名单。
# assistant / system / tool_event / approval 属内部审计消息，只能由服务端写入；
# 此前外部接口接受全部角色，客户端可伪造工具执行审计与助手回复。
CLIENT_MESSAGE_ROLES = {"user"}


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
        # 外部追加消息仅允许 user 角色（P0-4 修复）；内部角色由服务端各业务路径写入
        if v not in CLIENT_MESSAGE_ROLES:
            raise ValueError(
                f"外部追加消息仅允许 role={sorted(CLIENT_MESSAGE_ROLES)}；"
                "assistant/system/tool_event/approval 为服务端内部角色"
            )
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
