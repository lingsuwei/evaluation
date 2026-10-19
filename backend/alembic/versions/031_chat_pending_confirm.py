"""031：chat_sessions 新增 pending_confirm 字段。

写操作工具（create_llm_model/create_agent 等）调用后，agent loop 暂停等待
用户确认。pending_confirm 存储待确认的操作类型和参数，confirm/cancel 端点
读取后执行或取消，然后恢复 agent loop。

服务重启时 startup 清理孤儿 pending_confirm（Future 已丢失，协程已死）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "031_chat_pending_confirm"
down_revision: Union[str, None] = "030_chat_session_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return column in {c["name"] for c in inspect(bind).get_columns(table)}


def upgrade() -> None:
    if not _has_column("chat_sessions", "pending_confirm"):
        op.add_column(
            "chat_sessions",
            sa.Column("pending_confirm", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("chat_sessions", "pending_confirm"):
        op.drop_column("chat_sessions", "pending_confirm")
