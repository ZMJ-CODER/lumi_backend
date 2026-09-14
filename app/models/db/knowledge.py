"""``app.models.db.knowledge``：知识域数据：知识空间、文档与分块；本地项目与代码索引（不含代码正文）。

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
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.config import settings
from app.models.db_base import UUIDMixin, Base

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查/ruff 提供名字
    from app.models.db.user import User

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
    # Celery 是至少一次投递，领取状态必须持久化，不能只依赖 worker 内存。
    celery_task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
        Index("idx_chunks_embedding", "embedding", postgresql_using="ivfflat", postgresql_with={"lists": 100}, postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


# ── 办公文档临时会话（聊天框上传，短期保留；知识空间文档走 documents 表长期保留） ──

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


# ── 任务控制面 / 步骤检查点的异步数据库投影（方案 §3.3）──────────
#
# 这两张表**不是**实时事实源：事实源是 Redis Job State + ResultStore + Effect Journal。
# 它们只被异步投影器消费（批量、幂等 upsert），用于查询与审计；DB 写失败不阻塞任务
# 执行，恢复后可补写。因此这里只放"可查询的紧凑字段"，绝不放完整结果正文。


