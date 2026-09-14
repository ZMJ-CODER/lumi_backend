"""``app.models.db.memory``：长期记忆：L1 事实（``memories``）与用户画像（``memory_profile``）。

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
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.config import settings
from app.models.db_base import UUIDMixin, Base

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查/ruff 提供名字
    from app.models.db.user import User

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

