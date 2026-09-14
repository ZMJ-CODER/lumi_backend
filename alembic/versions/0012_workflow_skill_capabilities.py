"""add capability declarations to private workflow skills

Revision ID: 0012_workflow_skill_capabilities
Revises: 0011_user_workflow_skills
Create Date: 2026-09-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0012_workflow_skill_capabilities"
down_revision = "0011_user_workflow_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("user_workflow_skills")}
    if "provided_goals" not in columns:
        op.add_column("user_workflow_skills", sa.Column("provided_goals", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"))
    if "provided_sources" not in columns:
        op.add_column("user_workflow_skills", sa.Column("provided_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"))
    if "safety_level" not in columns:
        op.add_column("user_workflow_skills", sa.Column("safety_level", sa.String(length=20), nullable=False, server_default="READ_ONLY"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("user_workflow_skills")}
    for name in ("safety_level", "provided_sources", "provided_goals"):
        if name in columns:
            op.drop_column("user_workflow_skills", name)
