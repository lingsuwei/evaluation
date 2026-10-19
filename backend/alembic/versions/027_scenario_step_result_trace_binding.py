"""027：scenario_step_results 新增 trace_binding JSON 列。

矩阵快路径（scenario_eval_service.score_run_against_scenario 快路径）原本写死
trace_binding: {} 返回空 dict，导致 drawer 展示「该 observation 无 Input/Output」。
持久化 trace_binding 后，矩阵可直接返回真实绑定数据，避免 Langfuse HTTP 调用。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "027_ssr_trace_binding"
down_revision: Union[str, None] = "026_trace_sync_incomplete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("scenario_step_results", "trace_binding"):
        op.add_column(
            "scenario_step_results",
            sa.Column("trace_binding", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("scenario_step_results", "trace_binding"):
        op.drop_column("scenario_step_results", "trace_binding")
