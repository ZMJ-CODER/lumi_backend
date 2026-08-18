"""对话模块数据模型."""

from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    scene: str = Field(default="chat", description="场景标识")
    title: str | None = Field(default=None, description="会话标题（缺省为 新会话）")
    client_conversation_id: str | None = Field(
        default=None, description="客户端生成的会话 ID（UUID，用于幂等创建）"
    )


class SendMessageRequest(BaseModel):
    conversation_id: str | None = Field(
        default=None, description="会话 ID（流式接口从请求体取；阻塞接口走路径参数）"
    )
    content: str = Field(..., description="消息内容")
    display_content: str | None = Field(
        default=None,
        description="用户可见的原始消息；content 可额外携带文档上下文，但不得用于气泡展示",
    )
    scene: str = Field(default="office")
    local_mode: bool = Field(default=False)
    attachments: list = Field(default_factory=list)
    office_docs: list[dict] = Field(
        default_factory=list,
        description="办公模式挂载的结构化文档，仅 office 场景交给 DAG Planner",
    )
    guest_id: str | None = Field(default=None, description="游客身份标识（未登录时由前端生成，登录后忽略）")
    retrieval_query: str | None = Field(
        default=None,
        description="检索用查询（本地小模型精炼改写后的版本）；缺省时用 content 检索",
    )
    web_search: bool = Field(default=False, description="是否开启联网搜索（Tavily）")
    thinking_mode: str = Field(
        default="fast",
        description="推理模式：fast=低推理（快速回复）/ think=高推理（深度思考）；仅影响普通聊天主回复",
    )
    reply_style: str | None = Field(
        default=None,
        description="回复风格：long=长句整体回复 / short=多条短句分段回复；仅普通模式（chat）生效",
    )
    message_id: str | None = Field(
        default=None, description="客户端消息 ID（UUID，用于幂等去重，防止重复提交）"
    )
    regenerate: bool = Field(
        default=False,
        description="重新生成：先删除 replace_message_id / replace_client_message_id 对应的旧消息对，再生成新回复",
    )
    replace_message_id: str | None = Field(
        default=None, description="重新生成时被替换的旧 AI 回复消息 ID（服务端消息 ID）"
    )
    replace_client_message_id: str | None = Field(
        default=None, description="重新生成时被替换的旧用户消息客户端 ID（对应本地消息 id）"
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
