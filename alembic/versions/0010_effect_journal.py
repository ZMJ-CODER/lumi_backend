"""Durable two-phase journal for externally visible operations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_effect_journal"
down_revision = "0009_rag_keyword_trigram"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``create_all`` was used by older deployments before Alembic became the
    # single schema owner.  Such databases can already contain this table while
    # still being stamped at revision 0009.  Reusing it is safe and lets Alembic
    # advance to 0010 instead of failing with DuplicateTableError.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("effect_journal"):
        op.create_table(
            "effect_journal",
            sa.Column("idempotency_key", sa.String(length=160), nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("node_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("tool", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("params_sha256", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("intent_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("reason", sa.String(length=160), nullable=True),
            sa.Column("intent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("uncertain_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint("status IN ('intent', 'confirmed', 'uncertain')", name="ck_effect_journal_status"),
            sa.PrimaryKeyConstraint("idempotency_key"),
        )

    # Index creation is also idempotent for tables created by the legacy path.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_effect_journal_job "
        "ON effect_journal (job_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_effect_journal_status_intent "
        "ON effect_journal (status, intent_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_effect_journal_status_intent")
    op.execute("DROP INDEX IF EXISTS idx_effect_journal_job")
    op.execute("DROP TABLE IF EXISTS effect_journal")
