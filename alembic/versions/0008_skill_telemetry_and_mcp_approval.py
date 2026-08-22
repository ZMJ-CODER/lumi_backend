"""add Skill telemetry and MCP binding approval state

Revision ID: 0008_skill_telemetry
Revises: 0007_mcp_binding_quotas
Create Date: 2026-08-21
"""

from alembic import op


revision = "0008_skill_telemetry"
down_revision = "0007_mcp_binding_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_telemetry_daily (
            id UUID PRIMARY KEY,
            metric_date DATE NOT NULL,
            skill_name VARCHAR(200) NOT NULL,
            skill_version VARCHAR(32) NOT NULL DEFAULT '1.0.0',
            scene VARCHAR(32) NOT NULL,
            error_class VARCHAR(80) NOT NULL DEFAULT 'none',
            calls INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            duration_ms_total INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_skill_telemetry_daily_bucket
                UNIQUE (metric_date, skill_name, skill_version, scene, error_class)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_telemetry_daily_lookup "
        "ON skill_telemetry_daily (skill_name, skill_version, scene, metric_date)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS skill_telemetry_daily")
