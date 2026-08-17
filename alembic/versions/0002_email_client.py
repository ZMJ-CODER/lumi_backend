"""add email_client preference column

Revision ID: 0002_email_client
Revises: 0001_baseline
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_email_client"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为 user_preferences 增加 email_client 列（幂等：已存在则跳过）."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "ALTER TABLE user_preferences "
                "ADD COLUMN IF NOT EXISTS email_client VARCHAR(32) NOT NULL DEFAULT ''"
            )
        )
    else:
        try:
            with op.batch_alter_table("user_preferences") as batch:
                batch.add_column(
                    sa.Column("email_client", sa.String(32), nullable=False, server_default="")
                )
        except Exception:  # noqa: BLE001 列已存在（老库直接建过）
            pass


def downgrade() -> None:
    """回滚：删除 email_client 列."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE user_preferences DROP COLUMN IF EXISTS email_client"))
    else:
        with op.batch_alter_table("user_preferences") as batch:
            batch.drop_column("email_client")
