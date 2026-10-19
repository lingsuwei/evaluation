"""005b：编排表（历史分支占位，已合并至 005_p5 主线）。

部分生产库曾以本 revision 自 004 分叉；保留空迁移以便 Alembic 合并分支。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005b_create_orchestration_tables"
down_revision: Union[str, None] = "004_p3_experiment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
