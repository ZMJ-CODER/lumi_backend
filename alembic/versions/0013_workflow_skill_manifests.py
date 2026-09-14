"""add execution manifests to private workflow skills

Revision ID: 0013_workflow_skill_manifests
Revises: 0012_workflow_skill_capabilities
Create Date: 2026-09-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_workflow_skill_manifests"
down_revision = "0012_workflow_skill_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("user_workflow_skills")}
    additions = {
        "dependencies": sa.Column("dependencies", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        "execution_scope": sa.Column("execution_scope", sa.String(length=40), nullable=False, server_default="backend"),
        "availability_policy": sa.Column("availability_policy", sa.String(length=40), nullable=False, server_default="fail_if_missing"),
        "fallback_policy": sa.Column("fallback_policy", sa.String(length=40), nullable=False, server_default="clarify"),
        "approval_policy": sa.Column("approval_policy", sa.String(length=40), nullable=False, server_default="none"),
        "prompt_body": sa.Column("prompt_body", sa.Text(), nullable=False, server_default=""),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("user_workflow_skills", column)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    columns = {column["name"] for column in sa.inspect(bind).get_columns("user_workflow_skills")}
    for name in ("prompt_body", "approval_policy", "fallback_policy", "availability_policy", "execution_scope", "dependencies"):
        if name in columns:
            op.drop_column("user_workflow_skills", name)
