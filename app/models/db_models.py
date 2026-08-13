"""数据库 ORM 模型 —— 对应设计文档 5.1 核心表结构.

表:
  - users              用户表
  - conversations      会话表
  - messages           消息表
  - knowledge_spaces   知识空间表
  - documents          文档表
  - document_chunks    文档分块 + 向量表 (pgvector)
  - memories           长期记忆表
  - control_logs       操控日志表
"""

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.models.db_base import UUIDMixin, Base


# ── 用户表 ────────────────────────────────────────────

class User(Base, UUIDMixin):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(100), nullable=False, comment="用户昵称")
    account: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, comment="登录账号（邮箱/手机号）")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20),
        CheckConstraint("role IN ('superadmin', 'admin', 'user')"),
        default="user",
    )
    prompt_id: Mapped[str | None] = mapped_column(
        String(50), comment="用户选定的角色提示词 id（null=场景默认）"
    )
    avatar_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / disabled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    # 关联
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user", lazy="selectin")
    memories: Mapped[list["Memory"]] = relationship(back_populates="user", lazy="selectin")
    memory_profile: Mapped["MemoryProfile | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    knowledge_spaces: Mapped[list["KnowledgeSpace"]] = relationship(back_populates="owner", lazy="selectin")


# ── 会话表 ────────────────────────────────────────────

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

class KnowledgeSpace(Base, UUIDMixin):
    __tablename__ = "knowledge_spaces"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    scene_tag: Mapped[str | None] = mapped_column(String(50))  # office / game / chat / python 等
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped["User"] = relationship(back_populates="knowledge_spaces")
    documents: Mapped[list["Document"]] = relationship(back_populates="space", lazy="selectin")


# ── 文档表 ────────────────────────────────────────────

class Document(Base, UUIDMixin):
    __tablename__ = "documents"

    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_spaces.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    file_size: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / processing / ready / error
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str | None] = mapped_column(String(50), default="general")  # news / general / history / other（时效档次）
    tags: Mapped[str | None] = mapped_column(Text)  # 开放主题标签（逗号分隔，如 "科技, 发布会"）
    error_message: Mapped[str | None] = mapped_column(Text)  # 处理失败原因（质量门/解析失败时写入）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    space: Mapped["KnowledgeSpace"] = relationship(back_populates="documents")


# ── 文档分块 + 向量表 ──────────────────────────────────

class DocumentChunk(Base, UUIDMixin):
    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_spaces.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBEDDING_DIMENSION))
    metadata_: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_chunks_user_space", "user_id", "space_id"),
        Index("idx_chunks_embedding", "embedding", postgresql_using="ivfflat", postgresql_with={"lists": 100}, postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


# ── 长期记忆表 ─────────────────────────────────────────

class Memory(Base, UUIDMixin):
    __tablename__ = "memories"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    fact: Mapped[str] = mapped_column(Text, nullable=False)          # 注入文本：L0 明文 / L1 占位符
    fact_encrypted: Mapped[str | None] = mapped_column(Text)         # L1 密文 base64(nonce||ct||tag)
    fact_indexable: Mapped[str | None] = mapped_column(Text)         # L1 占位符（向量化/关键词检索对象）
    memory_type: Mapped[str] = mapped_column(String(20), default="experience")  # identity/preference/experience/goal
    privacy_level: Mapped[int] = mapped_column(SmallInteger, default=0)  # 0 普通 / 1 私密（L2 不落库）
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBEDDING_DIMENSION))
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    key_version: Mapped[int] = mapped_column(Integer, default=1)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_accessed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="memories")

    __table_args__ = (
        Index("idx_memories_user_active", "user_id", "is_deleted", "expire_at"),
        Index(
            "idx_memories_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


# ── 用户记忆画像表 ────────────────────────────────────

class MemoryProfile(Base):
    """用户长期记忆画像（JSON，常驻注入 system prompt）."""

    __tablename__ = "memory_profile"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 画像 JSON，结构见设计文档 §7
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="memory_profile")


# ── 用户自定义角色提示词表 ────────────────────────────

class UserPrompt(Base, UUIDMixin):
    """用户自定义角色提示词（可插拔；内置角色在 app/prompts/*.md）."""

    __tablename__ = "user_prompts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_user_prompts_user", "user_id"),
    )


# ── LLM token 用量：原始记录 + 每日聚合 ───────────────

class LLMUsage(Base, UUIDMixin):
    """每次 LLM 调用的 token 用量原始记录（按用户 × 用途 × 模型）."""

    __tablename__ = "llm_usage"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, comment="用途：chat/memory_extract/summary 等")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_llm_usage_user_cat_created", "user_id", "category", "created_at"),
    )


class DailyTokenStat(Base, UUIDMixin):
    """每日聚合的 token 用量（用户 × 日期 × 用途 × 模型），供低成本查询."""

    __tablename__ = "daily_token_stats"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    call_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "stat_date", "category", "model", name="uq_daily_token_stats"
        ),
    )


# ── refresh_tokens 表 ─────────────────────────────────

class RefreshToken(Base, UUIDMixin):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 操控日志表 ─────────────────────────────────────────

class ControlLog(Base, UUIDMixin):
    __tablename__ = "control_logs"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)  # open_app / volume_set / read_file 等
    target: Mapped[str | None] = mapped_column(String(500))
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base, UUIDMixin):
    """本地项目（方案 A）：代码留在用户端，服务器只存结构索引."""

    __tablename__ = "projects"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    root_label: Mapped[str | None] = mapped_column(String(500))
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ready", nullable=False)
    vector_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_projects_user", "user_id"),
    )


class ProjectIndex(Base, UUIDMixin):
    """项目结构索引：文件路径 + 符号 + 摘要（不含代码正文）."""

    __tablename__ = "project_index"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    symbols: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_project_index_project", "project_id"),
    )


class CodeEmbedding(Base, UUIDMixin):
    """本地代码向量：file_key=路径哈希（服务器不知真实路径与代码），供 agent 语义定位."""

    __tablename__ = "code_embeddings"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    file_key: Mapped[str] = mapped_column(String(64), nullable=False)
    function_name: Mapped[str | None] = mapped_column(String(200))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(String(1000))
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.EMBEDDING_DIMENSION), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_code_emb_project", "project_id"),
        Index(
            "idx_code_emb_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
