"""016：evaluation_tasks.process_eval_completed（离线任务 langfuse 过程回放完成标识）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "016_task_process_eval_completed"
down_revision: Union[str, None] = "015_agent_run_evaluation_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("evaluation_tasks", "process_eval_completed"):
        op.add_column(
            "evaluation_tasks",
            sa.Column(
                "process_eval_completed",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "process_eval_completed")
