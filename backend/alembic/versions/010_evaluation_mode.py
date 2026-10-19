"""010：evaluation_tasks.evaluation_mode（在线 / 离线评测模式）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "010_evaluation_mode"
down_revision: Union[str, None] = "009_scenario_step_enforce_order"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_tasks",
        sa.Column(
            "evaluation_mode",
            sa.String(),
            server_default="offline",
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE evaluation_tasks
        SET evaluation_mode = 'online'
        WHERE agent_id IS NOT NULL AND TRIM(agent_id) != ''
        """
    )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "evaluation_mode")
