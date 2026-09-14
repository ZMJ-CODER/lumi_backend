"""``app.models.db.job``：任务控制面：副作用日志（effect journal）与任务/步骤投影。

从 ``app/models/db_models.py`` 拆出。**关系目标写在注解字符串里**
（形状是 ``Mapped[list[类名]]``、以及 ``order_by=类名.字段``），由 SQLAlchemy
registry 按类名解析，因此这些类必须在 mapper 配置前全部 import——
``app/models/db/__init__.py`` 负责保证这一点（聚合入口 ``app.models.db_models`` 也会）。
"""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.db_base import Base

class EffectJournal(Base):
    """Durable reservation/confirmation record for one external operation.

    ``intent_payload`` contains only the tool identity and parameter digest;
    full request bodies stay in normal job state and are never copied here.
    """

    __tablename__ = "effect_journal"

    idempotency_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    node_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tool: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    params_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    intent_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_payload: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(160))
    intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uncertain_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # ── 方案《结果存储、检查点与恢复》§4.2：副作用的可判定信息 ──
    # effect_type 决定"怎么核对"（文件类查目标路径、发送类查消息 ID）；
    # effect_key 是幂等键（目标路径 hash / 消息 ID），重试前先查它避免重复副作用。
    step_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    effect_type: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="")
    effect_key: Mapped[str] = mapped_column(String(160), nullable=False, default="", server_default="")
    result_ref: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "status IN ('intent', 'pending', 'confirmed', 'uncertain')",
            name="ck_effect_journal_status",
        ),
        Index("idx_effect_journal_job", "job_id"),
        Index("idx_effect_journal_status_intent", "status", "intent_at"),
        Index("idx_effect_journal_effect_key", "effect_key"),
    )


# ── 用户外部 MCP 工具绑定 ────────────────────────────────

class JobRun(Base):
    """任务控制面投影（``job_runs``）：一条任务一行。"""

    __tablename__ = "job_runs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    current_step_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 已落盘检查点的最大版本：恢复时与 plan_revision 对比，防止旧检查点覆盖新状态。
    last_checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_job_runs_user_status", "user_id", "status"),
        Index("idx_job_runs_conversation", "conversation_id"),
    )


class JobStep(Base):
    """步骤检查点投影（``job_steps``）：唯一键 ``(job_id, step_id, attempt)``。

    字段与契约 ``StepCheckpoint`` 一一对应；``result_ref`` 只存**最小引用**
    （``{"id", "sha256"}``），完整正文永远在 ResultStore / 产物存储里。
    """

    __tablename__ = "job_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    step_type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="planned", index=True)
    input_digest: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    output_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_ref: Mapped[dict | None] = mapped_column(JSONB)
    artifact_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error_code: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    effect_status: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("job_id", "step_id", "attempt", name="uq_job_steps_job_step_attempt"),
        Index("idx_job_steps_job_status", "job_id", "status"),
        Index("idx_job_steps_checkpoint_version", "job_id", "checkpoint_version"),
    )
