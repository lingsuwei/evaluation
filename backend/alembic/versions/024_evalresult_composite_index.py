"""024：evaluation_results 新增 (task_id, metric_name) 复合索引。

detail 接口及其子函数频繁按 task_id + metric_name 过滤 EvaluationResult，
单列索引 task_id 需回表后再过滤 metric_name。复合索引可走覆盖扫描，消除回表过滤。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "024_evalresult_composite_index"
down_revision: Union[str, None] = "023_task_dimension_enable_flags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_evaluation_results_task_id_metric_name",
        "evaluation_results",
        ["task_id", "metric_name"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_evaluation_results_task_id_metric_name",
        table_name="evaluation_results",
    )
