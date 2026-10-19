"""022：agents 表新增 agent_kind 列（single | workflow）。

工作流评测适配（W1-1）：以 agent_kind 作 Agent 类型判别，
single=单 Agent（默认，兼容存量），workflow=多 Agent 协作工作流。
NOT NULL + server_default='single' 保证存量行回填，加索引支持过滤。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "022_agent_kind"
down_revision: Union[str, None] = "021_custom_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    return index in {idx["name"] for idx in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    if not _has_column("agents", "agent_kind"):
        op.add_column(
            "agents",
            sa.Column(
                "agent_kind",
                sa.String(),
                nullable=False,
                server_default="single",
            ),
        )
    if not _has_index("agents", "ix_agents_agent_kind"):
        op.create_index("ix_agents_agent_kind", "agents", ["agent_kind"])


def downgrade() -> None:
    if _has_index("agents", "ix_agents_agent_kind"):
        op.drop_index("ix_agents_agent_kind", table_name="agents")
    if _has_column("agents", "agent_kind"):
        op.drop_column("agents", "agent_kind")
