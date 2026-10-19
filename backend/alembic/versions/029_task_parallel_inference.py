"""029：evaluation_tasks 新增 parallel_inference 在线推理并发开关。

- True（默认）= 题目级 + 轮次级并发下发到 Agent（保持旧行为）
- False = 题目与轮次全串行下发，适合小参数模型 Agent，避免并发干扰
server_default=true 回填存量任务，保持行为兼容。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "029_task_parallel_inference"
down_revision: Union[str, None] = "028_migrate_judging_mode_names"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("evaluation_tasks", "parallel_inference"):
        op.add_column(
            "evaluation_tasks",
            sa.Column("parallel_inference", sa.Boolean(), nullable=True, server_default=sa.true()),
        )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "parallel_inference")
