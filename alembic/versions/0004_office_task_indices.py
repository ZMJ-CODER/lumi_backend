"""add persistent recent office task index

Revision ID: 0004_office_task_indices
Revises: 0003_conversation_memory_layers
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_office_task_indices"
down_revision: Union[str, None] = "0003_conversation_memory_layers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS office_task_indices (
            job_id VARCHAR(64) PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id VARCHAR(64),
            status VARCHAR(24) NOT NULL,
            request_summary TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '',
            input_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            artifact_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            result_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_office_task_indices_user_conv_completed "
        "ON office_task_indices (user_id, conversation_id, completed_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_office_task_indices_user_status_completed "
        "ON office_task_indices (user_id, status, completed_at DESC)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TABLE IF EXISTS office_task_indices")
