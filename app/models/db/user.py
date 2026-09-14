"""``app.models.db.user``：用户与账号：身份、刷新令牌、自定义提示词与个性化偏好。

从 ``app/models/db_models.py`` 拆出。**关系目标写在注解字符串里**
（形状是 ``Mapped[list[类名]]``、以及 ``order_by=类名.字段``），由 SQLAlchemy
registry 按类名解析，因此这些类必须在 mapper 配置前全部 import——
``app/models/db/__init__.py`` 负责保证这一点（聚合入口 ``app.models.db_models`` 也会）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import uuid
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models.db_base import UUIDMixin, Base

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查/ruff 提供名字
    from app.models.db.conversation import Conversation
    from app.models.db.knowledge import KnowledgeSpace
    from app.models.db.memory import Memory, MemoryProfile

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


# ── 用户个性化偏好（多端同步，按用户隔离） ──────────────

class UserPreference(Base):
    """用户个性化偏好：每个用户一行（智能体头像 / 全局背景 / 回复风格 / 声音设置）.

    首次使用为空（服务端返回默认值）；修改后保存到服务器，多端登录自动同步；
    所有字段按 user_id 隔离，互不影响。
    """

    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    avatar: Mapped[str | None] = mapped_column(Text)            # 智能体头像 dataURL
    background_image: Mapped[str | None] = mapped_column(Text)  # 全局主题背景 dataURL
    reply_style: Mapped[str] = mapped_column(String(16), default="long")  # long / short
    voice: Mapped[str | None] = mapped_column(Text)  # JSON: {voice, rate, pitch, referenceAudio, referenceName}
    email_client: Mapped[str] = mapped_column(
        String(32), default="", server_default=""
    )  # 默认邮件客户端：outlook/thunderbird/foxmail/mailmaster 等，空=系统默认
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── 用户保存的方案（角色 / 声音预设，可命名切换） ────────

class UserPreset(Base, UUIDMixin):
    """用户保存的个性化方案：kind=character（角色+回复风格） / voice（声音设置）."""

    __tablename__ = "user_presets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # character / voice
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)     # JSON 内容
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_user_presets_user_kind", "user_id", "kind"),
    )


# ── LLM token 用量：原始记录 + 每日聚合 ───────────────

class RefreshToken(Base, UUIDMixin):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── 操控日志表 ─────────────────────────────────────────

