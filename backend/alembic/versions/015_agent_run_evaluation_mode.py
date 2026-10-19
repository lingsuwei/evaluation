"""015：agent_runs.evaluation_mode 标识，区分在线推理与离线 stub（langfuse 过程回放）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "015_agent_run_evaluation_mode"
down_revision: Union[str, None] = "014_merge_orchestration_branch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("agent_runs", "evaluation_mode"):
        op.add_column(
            "agent_runs",
            sa.Column(
                "evaluation_mode",
                sa.String(),
                server_default="online",
                nullable=True,
            ),
        )
        op.execute(
            """
            UPDATE agent_runs
            SET evaluation_mode = 'online'
            WHERE origin = 'evaluation' AND (evaluation_mode IS NULL OR evaluation_mode = '')
            """
        )


def downgrade() -> None:
    op.drop_column("agent_runs", "evaluation_mode")
