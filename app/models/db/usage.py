"""``app.models.db.usage``：用量计量：LLM 调用原始记录与按日聚合。

从 ``app/models/db_models.py`` 拆出。**关系目标写在注解字符串里**
（形状是 ``Mapped[list[类名]]``、以及 ``order_by=类名.字段``），由 SQLAlchemy
registry 按类名解析，因此这些类必须在 mapper 配置前全部 import——
``app/models/db/__init__.py`` 负责保证这一点（聚合入口 ``app.models.db_models`` 也会）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.db_base import UUIDMixin, Base

class LLMUsage(Base, UUIDMixin):
    """每次 LLM 调用的 token 用量原始记录（按用户 × 用途 × 模型）."""

    __tablename__ = "llm_usage"

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False, comment="用途：chat/memory_extract/summary 等")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # ── 模型角色遥测（方案 §九.5；全部可空，老库未迁移时写入自动降级为只记旧字段）──
    model_role: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="逻辑角色：title/summary/intent_assessor/planner_* …"
    )
    model_profile: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="模型档位：main/cheap/reasoning/vision"
    )
    config_source: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="配置来源：default/env/admin/byok"
    )
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否发生角色/供应商回退")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, comment="本次调用耗时")
    structured_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="结构化输出是否通过校验")
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, comment="本次调用返回的工具调用数")
    complexity: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="任务复杂度 M0-M3/ATOMIC…")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_llm_usage_user_cat_created", "user_id", "category", "created_at"),
        Index("idx_llm_usage_role_created", "model_role", "created_at"),
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

