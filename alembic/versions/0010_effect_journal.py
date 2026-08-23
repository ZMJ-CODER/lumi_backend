"""Durable two-phase journal for externally visible operations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_effect_journal"
down_revision = "0009_rag_keyword_trigram"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.create_index("idx_effect_journal_job", "effect_journal", ["job_id"])
    op.create_index("idx_effect_journal_status_intent", "effect_journal", ["status", "intent_at"])


def downgrade() -> None:
    op.drop_index("idx_effect_journal_status_intent", table_name="effect_journal")
    op.drop_index("idx_effect_journal_job", table_name="effect_journal")
    op.drop_table("effect_journal")
