"""add external MCP binding quotas

Revision ID: 0007_mcp_binding_quotas
Revises: 0006_mcp_skill_lifecycle
Create Date: 2026-08-21
"""

from alembic import op


revision = "0007_mcp_binding_quotas"
down_revision = "0006_mcp_skill_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE user_mcp_tool_bindings "
        "ADD COLUMN IF NOT EXISTS daily_call_limit INTEGER NOT NULL DEFAULT 100"
    )
    op.execute(
        "ALTER TABLE user_mcp_tool_bindings "
        "ADD COLUMN IF NOT EXISTS concurrency_limit INTEGER NOT NULL DEFAULT 2"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE user_mcp_tool_bindings DROP COLUMN IF EXISTS concurrency_limit")
    op.execute("ALTER TABLE user_mcp_tool_bindings DROP COLUMN IF EXISTS daily_call_limit")
