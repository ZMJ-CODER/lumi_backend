"""add durable document task state

Revision ID: 0005_document_task_dispatch
Revises: 0004_office_task_indices
Create Date: 2026-08-21
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0005_document_task_dispatch"
down_revision: Union[str, None] = "0004_office_task_indices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR(64)")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ")
    op.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0")
    op.execute("CREATE INDEX IF NOT EXISTS idx_documents_celery_task_id ON documents (celery_task_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_documents_queued_at ON documents (queued_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_documents_processing_started_at ON documents (processing_started_at)")
    op.execute(
        """
        WITH ranked AS (
            SELECT ctid,
                   row_number() OVER (
                       PARTITION BY document_id, chunk_index
                       ORDER BY created_at DESC, ctid DESC
                   ) AS rank
            FROM document_chunks
        )
        DELETE FROM document_chunks chunks
        USING ranked
        WHERE chunks.ctid = ranked.ctid AND ranked.rank > 1
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_document_chunks_document_index "
        "ON document_chunks (document_id, chunk_index)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS uq_document_chunks_document_index")
    op.execute("DROP INDEX IF EXISTS idx_documents_processing_started_at")
    op.execute("DROP INDEX IF EXISTS idx_documents_queued_at")
    op.execute("DROP INDEX IF EXISTS idx_documents_celery_task_id")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS attempt_count")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS processing_started_at")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS queued_at")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS celery_task_id")
