"""``app.models.db.plugin``：插件与能力注册：用户批准的 MCP 工具、能力遥测、用户自建 Workflow Skill。

从 ``app/models/db_models.py`` 拆出。**关系目标写在注解字符串里**
（形状是 ``Mapped[list[类名]]``、以及 ``order_by=类名.字段``），由 SQLAlchemy
registry 按类名解析，因此这些类必须在 mapper 配置前全部 import——
``app/models/db/__init__.py`` 负责保证这一点（聚合入口 ``app.models.db_models`` 也会）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.db_base import UUIDMixin, Base

class UserMcpToolBinding(Base, UUIDMixin):
    """用户显式批准进入候选池的外部 MCP 工具。

    服务端地址始终引用部署配置中的 ``server_name``，不存用户提交的 URL，
    从数据模型上避免把 MCP 连接入口变成 SSRF 能力。
    """

    __tablename__ = "user_mcp_tool_bindings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    server_name: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    domain: Mapped[str] = mapped_column(String(80), nullable=False, default="external")
    intent_tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    scenes: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["office"])
    permission: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    write_op: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confirmation_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="server")
    idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resource_templates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    daily_call_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    concurrency_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="enabled")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "server_name", "raw_tool_name", name="uq_user_mcp_tool_binding"),
        Index("idx_user_mcp_tool_bindings_user_status", "user_id", "status"),
    )


class SkillTelemetryDaily(Base, UUIDMixin):
    """按能力版本聚合的非敏感运行遥测，供候选排序和运维观察使用。"""

    __tablename__ = "skill_telemetry_daily"

    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    skill_name: Mapped[str] = mapped_column(String(200), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    scene: Mapped[str] = mapped_column(String(32), nullable=False)
    error_class: Mapped[str] = mapped_column(String(80), nullable=False, default="none")
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "metric_date", "skill_name", "skill_version", "scene", "error_class",
            name="uq_skill_telemetry_daily_bucket",
        ),
        Index("idx_skill_telemetry_daily_lookup", "skill_name", "skill_version", "scene", "metric_date"),
    )


class UserWorkflowSkill(Base, UUIDMixin):
    """用户自建的声明式 Workflow Skill。

    不保存、导入或执行用户提交的 Python。用户只可定义任务说明、流程步骤和
    已审核 Tool 的白名单；所有真实调用仍经过统一 Tool Executor。
    """

    __tablename__ = "user_workflow_skills"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="user")
    scenes: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["office"])
    allowed_tools: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    dependencies: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    execution_scope: Mapped[str] = mapped_column(String(40), nullable=False, default="backend")
    availability_policy: Mapped[str] = mapped_column(String(40), nullable=False, default="fail_if_missing")
    fallback_policy: Mapped[str] = mapped_column(String(40), nullable=False, default="clarify")
    approval_policy: Mapped[str] = mapped_column(String(40), nullable=False, default="none")
    prompt_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Capability-first dispatcher declaration.  These are intentionally
    # independent of the Skill title/description so a user-created Skill can
    # enter office planning without adding planner code or keywords.
    provided_goals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    provided_sources: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    safety_level: Mapped[str] = mapped_column(String(20), nullable=False, default="READ_ONLY")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="enabled")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_user_workflow_skill_name"),
        CheckConstraint("status IN ('enabled', 'disabled')", name="ck_user_workflow_skill_status"),
        Index("idx_user_workflow_skills_user_status", "user_id", "status"),
    )


# ── 长期记忆表 ─────────────────────────────────────────

