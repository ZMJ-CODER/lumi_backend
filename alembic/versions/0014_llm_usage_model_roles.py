"""llm_usage 增加模型角色遥测列（方案 §九.5）

Revision ID: 0014_llm_usage_model_roles
Revises: 0013_workflow_skill_manifests
Create Date: 2026-09-12

新增列全部可空/有默认值，老数据不受影响；写入侧在未迁移时也会自动降级为
只记旧字段（``app/services/usage.py::record_usage``），因此本迁移不阻塞部署。
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_llm_usage_model_roles"
down_revision = "0013_workflow_skill_manifests"
branch_labels = None
depends_on = None


#: ``(列名, 类型, 可空, 服务端默认值)``。
#:
#: **NOT NULL 的列必须带 ``server_default``**：``llm_usage`` 是有历史数据的表，直接
#: ``ADD COLUMN ... NOT NULL`` 在**任何已有行**的库上都会失败
#: （``column "fallback_used" contains null values``）——本地库就是这样从 0011 卡住的，
#: 而线上只要有历史行就同样升不上去。默认值同时让"绕过 ORM 的直接 INSERT"也不会炸。
_COLUMNS = (
    ("model_role", sa.String(length=40), True, None),
    ("model_profile", sa.String(length=20), True, None),
    ("config_source", sa.String(length=40), True, None),
    ("fallback_used", sa.Boolean(), False, sa.text("false")),
    ("duration_ms", sa.Integer(), False, sa.text("0")),
    ("structured_ok", sa.Boolean(), True, None),
    ("tool_calls", sa.Integer(), False, sa.text("0")),
    ("complexity", sa.String(length=20), True, None),
)


def upgrade() -> None:
    for name, column_type, nullable, server_default in _COLUMNS:
        op.add_column(
            "llm_usage",
            sa.Column(name, column_type, nullable=nullable, server_default=server_default),
        )
    op.create_index(
        "idx_llm_usage_role_created", "llm_usage", ["model_role", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("idx_llm_usage_role_created", table_name="llm_usage")
    for name, _column_type, _nullable, _server_default in _COLUMNS:
        op.drop_column("llm_usage", name)
