"""``app.models.db.conversation``：会话与消息：会话、消息、会话记忆状态、会话摘要、附件。

从 ``app/models/db_models.py`` 拆出。**关系目标写在注解字符串里**
（形状是 ``Mapped[list[类名]]``、以及 ``order_by=类名.字段``），由 SQLAlchemy
registry 按类名解析，因此这些类必须在 mapper 配置前全部 import——
``app/models/db/__init__.py`` 负责保证这一点（聚合入口 ``app.models.db_models`` 也会）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid
from datetime import datetime
from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.config import settings
from app.models.db_base import UUIDMixin, Base

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查/ruff 提供名字
    from app.models.db.user import User

class Conversation(Base, UUIDMixin):
    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    scene: Mapped[str] = mapped_column(String(20), default="chat")  # chat / office / game
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", lazy="selectin", order_by="Message.created_at")

    __table_args__ = (
        # 会话列表高频查询：WHERE user_id = ? AND is_deleted = false ORDER BY updated_at DESC
        Index("idx_conversations_user_updated", "user_id", "is_deleted", text("updated_at DESC")),
    )


# ── 消息表 ────────────────────────────────────────────

class Message(Base, UUIDMixin):
    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user")  # user / assistant / system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[str | None] = mapped_column(Text)  # JSON string
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)  # JSON string
    client_message_id: Mapped[str | None] = mapped_column(String(64))  # 客户端消息 ID（幂等去重）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_messages_conv_created", "conversation_id", "created_at"),
        # 幂等：同一会话内客户端消息 ID 唯一（部分索引，兼容历史数据）
        Index(
            "uq_messages_conv_client",
            "conversation_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
    )


# ── 普通对话分层记忆 ────────────────────────────────────

class ConversationMemoryState(Base):
    """一段普通会话的紧凑状态。

    原始消息始终以 ``messages`` 为准；本表只保存可稳定注入的总摘要和
    已完成段摘要的游标，因此不会把长对话正文重复写进工作状态。
    """

    __tablename__ = "conversation_memory_states"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    processed_message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    global_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    open_loops: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    mood: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ConversationSegment(Base, UUIDMixin):
    """固定轮次的会话摘要，供历史话题检索和原文回捞使用。"""

    __tablename__ = "conversation_segments"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    message_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    entities: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    open_loops: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    mood: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBEDDING_DIMENSION))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    access_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_conversation_segments_sequence", "conversation_id", "sequence", unique=True),
        Index(
            "idx_conversation_segments_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ── 消息附件表 ───────────────────────────────────────

class Attachment(Base, UUIDMixin):
    """消息附件（图片/语音/视频等）.

    文件本体存服务器 uploads/chat/{user_id}/，file_url 为可访问的相对 URL；
    语音转文字等能力后续接入，type 字段预留 audio 类型。
    """

    __tablename__ = "attachments"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False, default="file")  # image / audio / video / file
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    message: Mapped["Message"] = relationship(back_populates="attachments")

    __table_args__ = (
        Index("idx_attachments_message", "message_id"),
    )


# ── 知识空间表 ─────────────────────────────────────────

