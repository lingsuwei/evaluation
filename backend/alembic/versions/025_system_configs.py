"""025：新增 system_configs 表（系统级配置 KV 存储）。

用于存储运行时指标全局默认阈值等可运行时修改的系统配置，
替代仅靠环境变量配置、需重启才能生效的限制。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "025_system_configs"
down_revision: Union[str, None] = "024_evalresult_composite_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "system_configs"):
        op.create_table(
            "system_configs",
            sa.Column("key", sa.String(), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("key"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "system_configs"):
        op.drop_table("system_configs")
