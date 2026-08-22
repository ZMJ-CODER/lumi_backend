"""add user MCP binding governance

Revision ID: 0006_mcp_skill_lifecycle
Revises: 0005_document_task_dispatch
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0006_mcp_skill_lifecycle"
down_revision: Union[str, None] = "0005_document_task_dispatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_mcp_tool_bindings (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            server_name VARCHAR(100) NOT NULL,
            raw_tool_name VARCHAR(200) NOT NULL,
            display_name VARCHAR(200) NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            input_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
            domain VARCHAR(80) NOT NULL DEFAULT 'external',
            intent_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
            scenes JSONB NOT NULL DEFAULT '["office"]'::jsonb,
            permission VARCHAR(20) NOT NULL DEFAULT 'user',
            write_op BOOLEAN NOT NULL DEFAULT TRUE,
            requires_confirmation BOOLEAN NOT NULL DEFAULT TRUE,
            confirmation_mode VARCHAR(20) NOT NULL DEFAULT 'server',
            idempotent BOOLEAN NOT NULL DEFAULT FALSE,
            resource_templates JSONB NOT NULL DEFAULT '[]'::jsonb,
            daily_call_limit INTEGER NOT NULL DEFAULT 100,
            concurrency_limit INTEGER NOT NULL DEFAULT 2,
            status VARCHAR(20) NOT NULL DEFAULT 'enabled',
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_user_mcp_tool_binding UNIQUE (user_id, server_name, raw_tool_name)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_mcp_tool_bindings_user_status "
        "ON user_mcp_tool_bindings (user_id, status)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TABLE IF EXISTS user_mcp_tool_bindings")
