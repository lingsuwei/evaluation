"""023：evaluation_tasks 新增 enable_process_consistency / enable_stability 维度开关。

发起评测任务时按需勾选是否评测 过程一致性 / 运行稳定性 维度。
- enable_process_consistency=False → Pipeline 跳过离线过程回放 + 结论 NA
- enable_stability=False → 结论 NA（不影响 inference_rounds）
两列均 server_default=true 回填存量任务，保持行为兼容。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "023_task_dimension_enable_flags"
down_revision: Union[str, None] = "022_agent_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("evaluation_tasks", "enable_process_consistency"):
        op.add_column(
            "evaluation_tasks",
            sa.Column("enable_process_consistency", sa.Boolean(), nullable=True, server_default=sa.true()),
        )
    if not _has_column("evaluation_tasks", "enable_stability"):
        op.add_column(
            "evaluation_tasks",
            sa.Column("enable_stability", sa.Boolean(), nullable=True, server_default=sa.true()),
        )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "enable_stability")
    op.drop_column("evaluation_tasks", "enable_process_consistency")
