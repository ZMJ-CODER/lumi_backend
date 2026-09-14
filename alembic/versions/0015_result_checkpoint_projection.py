"""结果存储 / 步骤检查点与恢复：副作用类型列 + job_runs / job_steps 投影表

Revision ID: 0015_result_checkpoint_projection
Revises: 0014_llm_usage_model_roles
Create Date: 2026-09-20

方案《结果存储、检查点与恢复（整合版）》§3.3 与 §4.2：

* ``effect_journal`` 增加 ``step_id`` / ``attempt`` / ``effect_type`` / ``effect_key`` /
  ``result_ref``：副作用的**可判定信息**（怎么核对、拿什么当幂等键）；
* 状态约束放宽到允许 ``pending``（与既有 ``intent`` 等价，主口径是 ``pending``）；
* 新建 ``job_runs`` / ``job_steps``：**异步投影**，不是实时事实源。

全部新增列都有默认值/可空，老数据不受影响；两张新表由投影器按需创建，
DB 不可用时投影失败不阻塞任务执行（方案 §3.3）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_result_checkpoint_projection"
down_revision = "0014_llm_usage_model_roles"
branch_labels = None
depends_on = None


_EFFECT_COLUMNS = (
    ("step_id", sa.String(length=128), False, ""),
    ("attempt", sa.Integer(), False, "1"),
    ("effect_type", sa.String(length=32), False, ""),
    ("effect_key", sa.String(length=160), False, ""),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ── 前置修复：``alembic_version.version_num`` 必须容得下 revision 标识 ──
    # Alembic 自己建表时用的是 ``VARCHAR(32)``，而本仓库的 revision 是
    # ``0015_result_checkpoint_projection``（33 字符）。于是**迁移本身必然失败**
    # （``StringDataRightTruncationError: 值太长了(32)``），而这发生在所有 DDL 之后、
    # 写版本号的时候——所以看起来像"某条 DDL 出错"，实际是版本号存不下。
    # 这里把它放宽到 64：只加宽、不改语义，且对已升级的库是幂等的 no-op。
    if inspector.has_table("alembic_version"):
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    if inspector.has_table("effect_journal"):
        existing = {column["name"] for column in inspector.get_columns("effect_journal")}
        for name, column_type, nullable, default in _EFFECT_COLUMNS:
            if name in existing:
                continue
            op.add_column(
                "effect_journal",
                sa.Column(name, column_type, nullable=nullable, server_default=default),
            )
        if "result_ref" not in existing:
            op.add_column(
                "effect_journal",
                sa.Column("result_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            )
        # 状态约束放宽：``pending`` 是主口径，``intent`` 继续被接受（历史行不迁移）。
        op.execute("ALTER TABLE effect_journal DROP CONSTRAINT IF EXISTS ck_effect_journal_status")
        op.execute(
            "ALTER TABLE effect_journal ADD CONSTRAINT ck_effect_journal_status "
            "CHECK (status IN ('intent', 'pending', 'confirmed', 'uncertain'))"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_effect_journal_effect_key "
            "ON effect_journal (effect_key)"
        )

    if not inspector.has_table("job_runs"):
        op.create_table(
            "job_runs",
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("conversation_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("current_step_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "last_checkpoint_version", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("error_code", sa.String(length=120), nullable=False, server_default=""),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.PrimaryKeyConstraint("job_id"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_user_status ON job_runs (user_id, status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_runs_conversation ON job_runs (conversation_id)"
    )

    if not inspector.has_table("job_steps"):
        op.create_table(
            "job_steps",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("step_id", sa.String(length=128), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("tool_name", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("step_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="planned"),
            sa.Column("input_digest", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("output_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("result_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column(
                "artifact_refs",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("error_code", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("effect_status", sa.String(length=24), nullable=False, server_default=""),
            sa.Column("checkpoint_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "step_id", "attempt", name="uq_job_steps_job_step_attempt"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_steps_job_status ON job_steps (job_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_steps_checkpoint_version "
        "ON job_steps (job_id, checkpoint_version)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_job_steps_checkpoint_version")
    op.execute("DROP INDEX IF EXISTS idx_job_steps_job_status")
    op.execute("DROP TABLE IF EXISTS job_steps")
    op.execute("DROP INDEX IF EXISTS idx_job_runs_conversation")
    op.execute("DROP INDEX IF EXISTS idx_job_runs_user_status")
    op.execute("DROP TABLE IF EXISTS job_runs")
    op.execute("DROP INDEX IF EXISTS idx_effect_journal_effect_key")
    op.execute("ALTER TABLE effect_journal DROP CONSTRAINT IF EXISTS ck_effect_journal_status")
    op.execute(
        "ALTER TABLE effect_journal ADD CONSTRAINT ck_effect_journal_status "
        "CHECK (status IN ('intent', 'confirmed', 'uncertain'))"
    )
    for name, _type, _nullable, _default in reversed(_EFFECT_COLUMNS):
        op.execute(f"ALTER TABLE effect_journal DROP COLUMN IF EXISTS {name}")
    op.execute("ALTER TABLE effect_journal DROP COLUMN IF EXISTS result_ref")
