"""``app.models.db.office``：办公域数据：办公文档会话与近期任务索引（不保存任务正文）。

从 ``app/models/db_models.py`` 拆出。**关系目标写在注解字符串里**
（形状是 ``Mapped[list[类名]]``、以及 ``order_by=类名.字段``），由 SQLAlchemy
registry 按类名解析，因此这些类必须在 mapper 配置前全部 import——
``app/models/db/__init__.py`` 负责保证这一点（聚合入口 ``app.models.db_models`` 也会）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.db_base import Base

class OfficeSession(Base):
    """聊天框上传的办公文档会话：DB 持久化（共享 Postgres，Docker/本地切换不丢）.

    两条链路设计：
      - 知识空间（设置/管理后台）上传 → documents + document_chunks 长期保留；
      - 聊天框上传 → office_sessions 临时保留（TTL + 前端轮次上限），
        磁盘 data/office/{user}/{doc_id} 仅为工作缓存，可随时从本表重建。
    """

    __tablename__ = "office_sessions"

    doc_id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="会话 id（12 位 hex）")
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="text", nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, comment="提取的全文（聊天注入 / RAG 索引用）")
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, comment="原始文件（编辑/保存用，可重建磁盘缓存）")
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True, comment="原始文件 SHA-256，用于显式晋升时去重")
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True, comment="关联会话（可选）")
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())


# ── 办公任务近期索引 ─────────────────────────────────────

class OfficeTaskIndex(Base):
    """办公任务跨请求定位索引，不保存任务正文或工具执行转录。"""

    __tablename__ = "office_task_indices"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    request_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_refs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    artifact_refs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    result_refs: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_office_task_indices_user_conv_completed", "user_id", "conversation_id", text("completed_at DESC")),
        Index("idx_office_task_indices_user_status_completed", "user_id", "status", text("completed_at DESC")),
    )


# ── 外部副作用两段式日志 ─────────────────────────────────

