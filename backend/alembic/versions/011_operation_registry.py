"""011：Operation Registry 表（agent_asset_snapshots + trace_operation_rules）。

A2a / §7.5.4：eval-owned Trace Binding Compiler 的持久化基础。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "011_operation_registry"
down_revision: Union[str, None] = "010_evaluation_mode"
branch_labels: Union[Sequence[str], None] = None
depends_on: Union[Sequence[str], None] = None


def upgrade() -> None:
    # 1. agent_asset_snapshots — Agent 运行态资产版本快照
    op.create_table(
        "agent_asset_snapshots",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("logical_agent_id", sa.String(), nullable=False),
        sa.Column("agent_version", sa.String(), nullable=True),
        sa.Column("asset_type", sa.String(), nullable=False),
        sa.Column("asset_hash", sa.String(), nullable=False),
        sa.Column("asset_source", sa.String(), nullable=True),
        sa.Column("asset_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("compile_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("compile_log", sa.Text(), nullable=True),
        sa.Column("registry_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "logical_agent_id",
            "asset_type",
            "asset_hash",
            name="uq_asset_snapshot_agent_type_hash",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_agent_asset_snapshots_logical_agent_id",
        "agent_asset_snapshots",
        ["logical_agent_id"],
        if_not_exists=True,
    )

    # 2. trace_operation_rules — eval-owned 业务操作绑定规则
    op.create_table(
        "trace_operation_rules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("logical_agent_id", sa.String(), nullable=False),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("source_asset_id", sa.String(), nullable=True),
        sa.Column("match_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("binding_reason_template", sa.String(), nullable=True),
        sa.Column("registry_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_asset_id"],
            ["agent_asset_snapshots.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "logical_agent_id",
            "operation_id",
            "registry_version",
            name="uq_operation_rule_agent_id_version",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_trace_operation_rules_logical_agent_id",
        "trace_operation_rules",
        ["logical_agent_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_trace_operation_rules_operation_id",
        "trace_operation_rules",
        ["operation_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_trace_operation_rules_source_asset_id",
        "trace_operation_rules",
        ["source_asset_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("trace_operation_rules")
    op.drop_table("agent_asset_snapshots")
