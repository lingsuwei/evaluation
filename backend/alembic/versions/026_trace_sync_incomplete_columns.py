"""026：trace_sync_jobs 新增 rerun_count / incomplete_metadata 列。

物化不完整（span_nodes=0 但 API 有 observations）时，独立计数 rerun_count
重试，超过上限标记 synced_incomplete。incomplete_metadata 记录历次校验失败
的 obs_count 等，供排查。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "026_trace_sync_incomplete"
down_revision: Union[str, None] = "025_system_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("trace_sync_jobs", "rerun_count"):
        op.add_column(
            "trace_sync_jobs",
            sa.Column("rerun_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("trace_sync_jobs", "incomplete_metadata"):
        op.add_column(
            "trace_sync_jobs",
            sa.Column("incomplete_metadata", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("trace_sync_jobs", "incomplete_metadata")
    op.drop_column("trace_sync_jobs", "rerun_count")
