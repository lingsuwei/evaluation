"""P3 Diff 表：run_comparisons + run_diffs

Revision ID: 003_p3_comparisons
Revises: 002_v_run_analytics
Create Date: 2026-05-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003_p3_comparisons"
down_revision: Union[str, None] = "002_v_run_analytics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_comparisons",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("comparison_scope", sa.String(), nullable=False, server_default="manual"),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("question_id", sa.Integer(), nullable=True),
        sa.Column("run_index", sa.Integer(), nullable=True),
        sa.Column("left_run_id", sa.String(), nullable=False),
        sa.Column("right_run_id", sa.String(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["left_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["right_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_comparisons_task_id", "run_comparisons", ["task_id"])
    op.create_index("ix_run_comparisons_left_run_id", "run_comparisons", ["left_run_id"])
    op.create_index("ix_run_comparisons_right_run_id", "run_comparisons", ["right_run_id"])

    op.create_table(
        "run_diffs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("comparison_id", sa.String(), nullable=False),
        sa.Column("diff_type", sa.String(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=False, server_default="1.0"),
        sa.Column("left_run_id", sa.String(), nullable=False),
        sa.Column("right_run_id", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["comparison_id"], ["run_comparisons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "comparison_id", "diff_type", name="uq_run_diff_per_comparison_type"
        ),
    )
    op.create_index("ix_run_diffs_comparison_id", "run_diffs", ["comparison_id"])
    op.create_index("ix_run_diffs_diff_type", "run_diffs", ["diff_type"])
    op.create_index("ix_run_diffs_left_run_id", "run_diffs", ["left_run_id"])
    op.create_index("ix_run_diffs_right_run_id", "run_diffs", ["right_run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_diffs_right_run_id", table_name="run_diffs")
    op.drop_index("ix_run_diffs_left_run_id", table_name="run_diffs")
    op.drop_index("ix_run_diffs_diff_type", table_name="run_diffs")
    op.drop_index("ix_run_diffs_comparison_id", table_name="run_diffs")
    op.drop_table("run_diffs")
    op.drop_index("ix_run_comparisons_right_run_id", table_name="run_comparisons")
    op.drop_index("ix_run_comparisons_left_run_id", table_name="run_comparisons")
    op.drop_index("ix_run_comparisons_task_id", table_name="run_comparisons")
    op.drop_table("run_comparisons")
