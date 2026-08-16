"""baseline: 按当前 ORM 模型建全量表（幂等，兼容已有库）

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-15
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """基线：create_all（CREATE TABLE IF NOT EXISTS 语义），
    已有库不受影响，新库一次到位；后续模型变更走 autogenerate diff."""
    from app.models.db_base import Base
    from app.models import db_models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    # 基线不回滚已有业务表（保守策略；历史数据不可逆）
    pass
