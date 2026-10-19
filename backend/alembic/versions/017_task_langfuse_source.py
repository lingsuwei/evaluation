"""017：evaluation_tasks.langfuse_source（离线任务 Langfuse 配置持久化 + 过程回放开关）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "017_task_langfuse_source"
down_revision: Union[str, None] = "016_task_process_eval_completed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("evaluation_tasks", "langfuse_source"):
        op.add_column(
            "evaluation_tasks",
            sa.Column("langfuse_source", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "langfuse_source")
