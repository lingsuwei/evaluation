"""020：评测报告归档表 evaluation_reports。

存放导出的评测报告(xlsx) 与 版本对比报告(html) 二进制及元数据，
供"评测报告"一级菜单列表展示与下载/预览。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "020_report_archive"
down_revision: Union[str, None] = "019_scenario_template_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluation_reports",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("report_type", sa.String(), nullable=False),  # evaluation | comparison
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),  # xlsx | html
        sa.Column("task_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("baseline_task_id", sa.String(), nullable=True),
        sa.Column("dataset_id", sa.String(), nullable=True),
        sa.Column("dataset_name", sa.String(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column("workspace_name", sa.String(), nullable=True),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verdict_status", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_evaluation_reports_dataset_id",
        "evaluation_reports",
        ["dataset_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_evaluation_reports_report_type",
        "evaluation_reports",
        ["report_type"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_evaluation_reports_created_at",
        "evaluation_reports",
        ["created_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("evaluation_reports")
