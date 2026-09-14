"""add private declarative workflow skills

Revision ID: 0011_user_workflow_skills
Revises: 0010_effect_journal
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_user_workflow_skills"
down_revision = "0010_effect_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or sa.inspect(bind).has_table("user_workflow_skills"):
        return
    op.create_table(
        "user_workflow_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=80), nullable=False, server_default="user"),
        sa.Column("scenes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("allowed_tools", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="enabled"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('enabled', 'disabled')", name="ck_user_workflow_skill_status"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_workflow_skill_name"),
    )
    op.create_index("idx_user_workflow_skills_user_status", "user_workflow_skills", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_user_workflow_skills_user_status", table_name="user_workflow_skills")
    op.drop_table("user_workflow_skills")
