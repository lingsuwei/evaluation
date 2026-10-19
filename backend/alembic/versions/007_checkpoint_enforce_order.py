"""007：过程点 enforce_order（启用顺序依赖）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007_checkpoint_enforce_order"
down_revision: Union[str, None] = "006_scenario_per_item_steps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "process_checkpoint_templates",
        sa.Column(
            "enforce_order",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("process_checkpoint_templates", "enforce_order")
