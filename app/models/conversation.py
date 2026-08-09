"""对话模块数据模型."""

from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    scene: str = Field(default="chat", description="场景标识")
    title: str | None = Field(default=None, description="会话标题（缺省为 新会话）")
    client_conversation_id: str | None = Field(
        default=None, description="客户端生成的会话 ID（UUID，用于幂等创建）"
    )


class SendMessageRequest(BaseModel):
    content: str = Field(..., description="消息内容")
    scene: str = Field(default="office")
    local_mode: bool = Field(default=False)
    attachments: list = Field(default_factory=list)
    guest_id: str | None = Field(default=None, description="游客身份标识（未登录时由前端生成，登录后忽略）")
    retrieval_query: str | None = Field(
        default=None,
        description="检索用查询（本地小模型精炼改写后的版本）；缺省时用 content 检索",
    )
    message_id: str | None = Field(
        default=None, description="客户端消息 ID（UUID，用于幂等去重，防止重复提交）"
    )


class UpdateConversationRequest(BaseModel):
    title: str = Field(..., description="新标题")


class Citation(BaseModel):
    """引用来源."""

    type: str  # personal / public
    title: str
    content: str
    source: str = ""


class MessageResponse(BaseModel):
    message_id: str
    content: str
    role: str = "assistant"
    citations: list[Citation] = Field(default_factory=list)
    created_at: str = ""
