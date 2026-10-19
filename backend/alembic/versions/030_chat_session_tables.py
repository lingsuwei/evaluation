"""030：新增 chat_sessions / chat_messages 表（AI 对话式配置）。

阶段 1：单轮 LLM 意图解析，draft_request 存 EvaluateRequest 草稿 JSON。
阶段 2：agent loop 增量填充 draft_request，ChatMessage 扩展 tool_* 字段。

down_revision 指向 029_task_parallel_inference，串联此前误共用 028 形成的多 head。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "030_chat_session_tables"
down_revision: Union[str, None] = "029_task_parallel_inference"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind, table_name: str) -> bool:
    return table_name in inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "chat_sessions"):
        op.create_table(
            "chat_sessions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("intent", sa.String(), nullable=True),
            sa.Column("model_key", sa.String(), nullable=True),
            sa.Column("intent_text", sa.Text(), nullable=True),
            sa.Column("draft_request", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("missing_fields", sa.Text(), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_chat_sessions_created_at", "chat_sessions", ["created_at"]
        )

    if not _has_table(bind, "chat_messages"):
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("session_id", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("tool_name", sa.String(), nullable=True),
            sa.Column("tool_args", sa.Text(), nullable=True),
            sa.Column("tool_result", sa.Text(), nullable=True),
            sa.Column("tokens", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_chat_messages_session_id", "chat_messages", ["session_id"]
        )
        op.create_index(
            "ix_chat_messages_created_at", "chat_messages", ["created_at"]
        )
        op.create_index(
            "ix_chat_messages_session_id_created_at",
            "chat_messages",
            ["session_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "chat_messages"):
        op.drop_table("chat_messages")
    if _has_table(bind, "chat_sessions"):
        op.drop_table("chat_sessions")
