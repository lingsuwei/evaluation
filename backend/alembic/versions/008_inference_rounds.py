"""008：evaluation_tasks.inference_rounds（每题 Agent 推理次数）。"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008_inference_rounds"
down_revision: Union[str, None] = "007_checkpoint_enforce_order"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_tasks",
        sa.Column(
            "inference_rounds",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("evaluation_tasks", "inference_rounds")
