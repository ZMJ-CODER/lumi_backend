"""add layered conversation memory tables

Revision ID: 0003_conversation_memory_layers
Revises: 0002_email_client
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003_conversation_memory_layers"
down_revision: Union[str, None] = "0002_email_client"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建分层会话记忆表；已有业务消息不迁移、不删除。"""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_memory_states (
            conversation_id UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            processed_message_count INTEGER NOT NULL DEFAULT 0,
            global_summary TEXT NOT NULL DEFAULT '',
            open_loops JSONB NOT NULL DEFAULT '[]'::jsonb,
            mood VARCHAR(500) NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_segments (
            id UUID PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            message_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary TEXT NOT NULL,
            entities JSONB NOT NULL DEFAULT '[]'::jsonb,
            open_loops JSONB NOT NULL DEFAULT '[]'::jsonb,
            mood VARCHAR(500) NOT NULL DEFAULT '',
            embedding vector(1024),
            source_hash VARCHAR(64) NOT NULL,
            model_version VARCHAR(100) NOT NULL DEFAULT '',
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_conversation_segments_sequence UNIQUE (conversation_id, sequence)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_segments_conversation_id "
        "ON conversation_segments (conversation_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_segments_embedding "
        "ON conversation_segments USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TABLE IF EXISTS conversation_segments")
        op.execute("DROP TABLE IF EXISTS conversation_memory_states")
