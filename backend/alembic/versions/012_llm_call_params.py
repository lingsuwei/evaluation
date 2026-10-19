"""Add max_tokens, temperature, timeout columns to llm_models

Revision ID: 012_llm_call_params
Revises: 011_operation_registry
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "012_llm_call_params"
down_revision = "011_operation_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_models", sa.Column("max_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_models", sa.Column("temperature", sa.Float(), nullable=True))
    op.add_column("llm_models", sa.Column("timeout", sa.Float(), nullable=True))
    # 设置默认值（已有行）
    op.execute("UPDATE llm_models SET max_tokens = 4096 WHERE max_tokens IS NULL")
    op.execute("UPDATE llm_models SET temperature = 0.0 WHERE temperature IS NULL")
    op.execute("UPDATE llm_models SET timeout = 180.0 WHERE timeout IS NULL")


def downgrade() -> None:
    op.drop_column("llm_models", "timeout")
    op.drop_column("llm_models", "temperature")
    op.drop_column("llm_models", "max_tokens")