"""Alembic 005：run_semantic_nodes（P5 Semantic Facts）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "005_p5_semantic_nodes"
down_revision: Union[str, None] = "004_p3_experiment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_semantic_nodes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("agent_run_id", sa.String(), nullable=False),
        sa.Column("observation_id", sa.String(), nullable=True),
        sa.Column("run_span_node_id", sa.String(), nullable=True),
        sa.Column("reasoning_summary", sa.Text(), nullable=True),
        sa.Column("embedding_id", sa.String(), nullable=True),
        sa.Column("generator_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_run_id",
            "observation_id",
            name="uq_run_semantic_observation",
        ),
    )
    op.create_index("ix_run_semantic_nodes_agent_run_id", "run_semantic_nodes", ["agent_run_id"])
    op.create_index("ix_run_semantic_nodes_observation_id", "run_semantic_nodes", ["observation_id"])
    op.create_index("ix_run_semantic_nodes_run_span_node_id", "run_semantic_nodes", ["run_span_node_id"])


def downgrade() -> None:
    op.drop_index("ix_run_semantic_nodes_run_span_node_id", table_name="run_semantic_nodes")
    op.drop_index("ix_run_semantic_nodes_observation_id", table_name="run_semantic_nodes")
    op.drop_index("ix_run_semantic_nodes_agent_run_id", table_name="run_semantic_nodes")
    op.drop_table("run_semantic_nodes")
