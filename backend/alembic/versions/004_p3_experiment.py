"""P3-B Experiment + trace_refs

Revision ID: 004_p3_experiment
Revises: 003_p3_comparisons
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "004_p3_experiment"
down_revision: Union[str, None] = "003_p3_comparisons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("baseline_agent_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("comparison_scope", sa.String(), nullable=False, server_default="version_experiment"),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_experiments_task_id", "experiments", ["task_id"])
    op.create_index("ix_experiments_baseline_agent_id", "experiments", ["baseline_agent_id"])

    op.create_table(
        "experiment_summaries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("experiment_id", sa.String(), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("comparison_id", sa.String(), nullable=True),
        sa.Column("left_agent_id", sa.String(), nullable=False),
        sa.Column("right_agent_id", sa.String(), nullable=False),
        sa.Column("left_run_id", sa.String(), nullable=False),
        sa.Column("right_run_id", sa.String(), nullable=False),
        sa.Column("outcome_winner", sa.String(), nullable=True),
        sa.Column("highlights", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["comparison_id"], ["run_comparisons.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "question_id",
            "right_agent_id",
            name="uq_experiment_summary_per_pair",
        ),
    )
    op.create_index("ix_experiment_summaries_experiment_id", "experiment_summaries", ["experiment_id"])

    op.create_table(
        "agent_run_trace_refs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_run_id", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="root"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "trace_id", name="uq_agent_run_trace_ref"),
    )
    op.create_index("ix_agent_run_trace_refs_agent_run_id", "agent_run_trace_refs", ["agent_run_id"])
    op.create_index("ix_agent_run_trace_refs_trace_id", "agent_run_trace_refs", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_trace_refs_trace_id", table_name="agent_run_trace_refs")
    op.drop_index("ix_agent_run_trace_refs_agent_run_id", table_name="agent_run_trace_refs")
    op.drop_table("agent_run_trace_refs")
    op.drop_index("ix_experiment_summaries_experiment_id", table_name="experiment_summaries")
    op.drop_table("experiment_summaries")
    op.drop_index("ix_experiments_baseline_agent_id", table_name="experiments")
    op.drop_index("ix_experiments_task_id", table_name="experiments")
    op.drop_table("experiments")
