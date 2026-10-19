"""P2 Runtime Fact Layer 基线表（agent_runs 扩展 + run_span_nodes + run_metrics + checkpoint_results）

Revision ID: 001_p2_fact
Revises:
Create Date: 2026-05-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_p2_fact"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("agent_runs", sa.Column("output_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.create_table(
        "run_span_nodes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_run_id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=True),
        sa.Column("sibling_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("depth", sa.Integer(), nullable=True),
        sa.Column("observation_id", sa.String(), nullable=True),
        sa.Column("node_type", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("aggregate_key", sa.String(), nullable=True),
        sa.Column("semantic_role", sa.String(), nullable=True),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("is_deterministic_io", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("mock_response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "observation_id", name="uq_run_span_observation"),
    )
    op.create_index("ix_run_span_nodes_agent_run_id", "run_span_nodes", ["agent_run_id"])
    op.create_index("ix_run_span_nodes_aggregate_key", "run_span_nodes", ["aggregate_key"])
    op.create_index("ix_run_span_nodes_semantic_role", "run_span_nodes", ["semantic_role"])

    op.create_table(
        "run_metrics",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_run_id", sa.String(), nullable=False),
        sa.Column("metric_key", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="runtime"),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dims", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dims_hash", sa.String(), nullable=False, server_default=""),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_run_id", "metric_key", "source", "dims_hash", name="uq_run_metric_dims"
        ),
    )
    op.create_index("ix_run_metrics_agent_run_id", "run_metrics", ["agent_run_id"])
    op.create_index("ix_run_metrics_metric_key", "run_metrics", ["metric_key"])

    op.create_table(
        "checkpoint_results",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_run_id", sa.String(), nullable=False),
        sa.Column("checkpoint_id", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "checkpoint_id", name="uq_checkpoint_result_per_run"),
    )
    op.create_index("ix_checkpoint_results_agent_run_id", "checkpoint_results", ["agent_run_id"])
    op.create_index("ix_checkpoint_results_checkpoint_id", "checkpoint_results", ["checkpoint_id"])

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_agent_runs_eval_context_gin ON agent_runs USING gin (eval_context)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_runs_eval_context_gin")
    op.drop_table("checkpoint_results")
    op.drop_table("run_metrics")
    op.drop_table("run_span_nodes")
    op.drop_column("agent_runs", "output_snapshot")
    op.drop_column("agent_runs", "input_snapshot")
