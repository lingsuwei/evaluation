"""006：按题场景模板 dataset_item_id、子步骤 enabled。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006_scenario_per_item_steps"
down_revision: Union[str, None] = "005_p5_semantic_nodes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scenario_eval_templates",
        sa.Column("dataset_item_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_scenario_eval_templates_dataset_item_id",
        "scenario_eval_templates",
        ["dataset_item_id"],
        unique=False,
    )
    op.add_column(
        "scenario_eval_steps",
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scenario_eval_steps", "enabled")
    op.drop_index(
        "ix_scenario_eval_templates_dataset_item_id",
        table_name="scenario_eval_templates",
    )
    op.drop_column("scenario_eval_templates", "dataset_item_id")
