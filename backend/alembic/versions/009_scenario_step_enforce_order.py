"""009：场景子步骤 enforce_order（启用顺序依赖）。"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_scenario_step_enforce_order"
down_revision: Union[str, None] = "008_inference_rounds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scenario_eval_steps",
        sa.Column(
            "enforce_order",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("scenario_eval_steps", "enforce_order")
