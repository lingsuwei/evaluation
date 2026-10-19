"""014：合并 005b 历史分支与 013 主线（消除 multiple heads）。"""
from typing import Sequence, Union

from alembic import op

revision: str = "014_merge_orchestration_branch"
down_revision: Union[str, tuple[str, ...], None] = (
    "013_evaluation_task_verdict_columns",
    "005b_create_orchestration_tables",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
