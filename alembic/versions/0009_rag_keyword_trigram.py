"""RAG keyword lookup indexes.

The query keeps ILIKE semantics for exact identifiers and Chinese text, while
pg_trgm prevents a full table scan as the chunk corpus grows.  It is a measured
intermediate step before adopting a model-provided sparse index.
"""

from alembic import op

revision = "0009_rag_keyword_trigram"
down_revision = "0008_skill_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE office_sessions ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_office_sessions_file_hash ON office_sessions (file_hash)")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_text_trgm "
        "ON document_chunks USING gin (chunk_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_filename_trgm "
        "ON documents USING gin (filename gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_documents_filename_trgm")
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_text_trgm")
    op.execute("DROP INDEX IF EXISTS idx_office_sessions_file_hash")
    op.execute("ALTER TABLE office_sessions DROP COLUMN IF EXISTS file_hash")
