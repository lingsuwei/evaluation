"""013：evaluation_tasks 四维结论与运行时目标列（PG 增量补全）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision: str = "013_evaluation_task_verdict_columns"
down_revision: Union[str, None] = "012_llm_call_params"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("evaluation_tasks", "runtime_metric_targets"):
        op.add_column("evaluation_tasks", sa.Column("runtime_metric_targets", sa.Text(), nullable=True))
    if not _has_column("evaluation_tasks", "verdict_thresholds"):
        op.add_column("evaluation_tasks", sa.Column("verdict_thresholds", sa.Text(), nullable=True))
    if not _has_column("evaluation_tasks", "auto_scenario_score"):
        op.add_column(
            "evaluation_tasks",
            sa.Column(
                "auto_scenario_score",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=True,
            ),
        )
    if not _has_column("evaluation_tasks", "resume_agent_id"):
        op.add_column("evaluation_tasks", sa.Column("resume_agent_id", sa.String(), nullable=True))
    if not _has_column("evaluation_tasks", "agent_ids"):
        op.add_column("evaluation_tasks", sa.Column("agent_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    for col in (
        "agent_ids",
        "resume_agent_id",
        "auto_scenario_score",
        "verdict_thresholds",
        "runtime_metric_targets",
    ):
        if _has_column("evaluation_tasks", col):
            op.drop_column("evaluation_tasks", col)
