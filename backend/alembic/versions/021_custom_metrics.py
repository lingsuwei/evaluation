"""021：自定义评测指标表 custom_metrics + evaluation_tasks.custom_metric_ids 列。

custom_metrics 表支持 geval/dag/code 三档自定义指标定义；
evaluation_tasks.custom_metric_ids 列关联任务选用的自定义指标 ID 列表（JSON array）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "021_custom_metrics"
down_revision: Union[str, None] = "020_report_archive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    return table in inspect(bind).get_table_names()


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    # 1. 创建 custom_metrics 表
    if not _has_table("custom_metrics"):
        op.create_table(
            "custom_metrics",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("category", sa.String(), nullable=True),
            sa.Column("criteria", sa.Text(), nullable=True),
            sa.Column("dag", sa.JSON(), nullable=True),
            sa.Column("code", sa.Text(), nullable=True),
            sa.Column("multi_turn", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("params", sa.JSON(), nullable=True),
            sa.Column("threshold", sa.Float(), server_default="0.5", nullable=False),
            sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    # 2. evaluation_tasks 新增 custom_metric_ids 列
    if not _has_column("evaluation_tasks", "custom_metric_ids"):
        op.add_column(
            "evaluation_tasks",
            sa.Column("custom_metric_ids", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("evaluation_tasks", "custom_metric_ids"):
        op.drop_column("evaluation_tasks", "custom_metric_ids")
    if _has_table("custom_metrics"):
        op.drop_table("custom_metrics")
